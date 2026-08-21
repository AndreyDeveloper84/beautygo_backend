"""Manual salon operations on bookings — DRF-1063 block D.

The audit of 2026-08-14 found no endpoint anywhere in Ayla where the
actor is a salon employee. The public ``create`` rejects everything that
is not a client, the internal one resolves the caller into a client
before it runs, and walk-in belongs to the master. So the three things a
front desk does all day — book someone in, move a booking, cancel one —
were reachable only by asking the customer to do it themselves, or by
the owner doing it by hand.

These are the same three commands the customer already has, with a
different actor and a different authorisation context. That is exactly
what ``Ayla MVP Appointment Contract`` §10 prescribes: manual/admin
booking "uses the same Appointment Domain and lifecycle as customer
booking. It differs by ``origin``, actor and authorization context. This
contract does not introduce ``OfflineAppointment``, ``ManualAppointment``
or a second booking aggregate." So nothing here re-implements booking:
the engine's advisory lock, conflict re-check, snapshot, TUR grant,
idempotency and outbox are reused unchanged, and this module supplies
the actor.

**Why a separate surface rather than widening ``AppointmentViewSet``.**
That viewset's ``get_queryset`` returns ``none()`` for an administrator.
Adding a third branch would make the salon's bookings visible to
``cancel`` and ``reschedule`` there too — but those actions derive their
initiator as ``"specialist" if request.user.is_specialist else "client"``,
so a salon cancellation would be recorded as the *client* changing their
mind, and priced with the client's cancellation fee. Widening the
queryset would have looked like the small change and silently produced
the wrong fact.

**Who the customer is.** Two ways, and no third:

* ``client_id`` — someone who already has an active relationship with
  this salon. Not "any user id in the system": a booking grants the
  tenant a relationship with that person, so accepting arbitrary ids
  would let one salon attach itself to strangers.
* ``client_name`` (+ optional ``client_phone``) — a new guest, recorded
  through the same proxy-account helper the master's walk-in path uses.

The phone here is *entered by* the salon, not *disclosed to* it, so
DRF-1039 is untouched — that decision is about Ayla not handing out
customers' numbers.
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.application.dto import (
    CancelBookingDTO,
    CreateBookingDTO,
    RescheduleBookingDTO,
)
from appointments.application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.exceptions import (
    AppointmentTerminalError,
    BookingWindowError,
    CancellationNotAllowedError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from appointments.domain.value_objects import (
    SALON_CANCELLATION_REASON_CODES,
    OperationalActor,
)
from appointments.models import Appointment
from appointments.serializers import AppointmentDetailSerializer
from users.permissions import IsProApp, IsTenantAdmin
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

# Writes on this surface stamp WHO acted onto the booking and its events.
# `IsTenantAdmin` rather than DRF-1062's `IsTenantAdminOrPlatformAdmin`:
# there is no canonical actor value for Ayla platform staff operating
# inside a tenant, and recording their action as `salon` would be false.
# Schedule edits (DRF-1062) are unattributed configuration, so the wider
# permission is right there and not here. Raised as an owner question.
_SALON_WRITE_PERMISSIONS = [
    permissions.IsAuthenticated,
    IsProApp,
    IsTenantAdmin,
]


class SalonBookingCreateSerializer(serializers.Serializer):
    """Validation only — the booking logic stays in CreateBookingService."""

    # The service is a REQUIRED input, never inferred and never
    # defaulted. Master Schedule UX Contract (15.08) fixes the order
    # Client → Service → Date/time → availability → review → create,
    # precisely because the service's duration decides which intervals
    # are usable at all. An endpoint that guessed it would be answering
    # a different question than the one the console asked.
    specialist_id = serializers.UUIDField()
    service_id = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()
    # Exactly one of the two identification paths; see module docstring.
    client_id = serializers.UUIDField(required=False)
    client_name = serializers.CharField(required=False, max_length=150)
    # Required WITH client_name, per the same contract: "минимальный
    # новый клиент — имя и телефон". Not required at field level because
    # it is meaningless on the client_id path.
    client_phone = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=20,
    )

    def validate(self, data):
        has_id = bool(data.get("client_id"))
        has_name = bool(data.get("client_name"))
        if has_id == has_name:
            raise serializers.ValidationError(
                "Provide exactly one of client_id (an existing customer of "
                "this salon) or client_name (a new guest)."
            )
        if has_name and not data.get("client_phone"):
            raise serializers.ValidationError(
                {"client_phone": "A new guest needs a name and a phone."}
            )
        return data

    def validate_client_phone(self, value):
        """Canonicalise to the same ``+7…`` shape a registered account
        stores, so the guest helper's "is this already a real account?"
        check compares like with like. Same rule as the master's walk-in
        serializer — see ``WalkInCreateSerializer.validate_client_phone``
        for why a non-RU-looking value is cleaned but accepted.
        """
        from appointments.serializers import WalkInCreateSerializer

        return WalkInCreateSerializer().validate_client_phone(value)


class SalonRescheduleSerializer(serializers.Serializer):
    """``expected_version`` is REQUIRED here.

    Optional on the mobile path only because app builds predating the
    field exist. The salon console has no such legacy: it reads the day
    journal — which carries every booking's ``version`` — immediately
    before offering the button, so a move without a version is a bug on
    the caller's side, not a compatibility case worth honouring.
    """

    new_start_datetime = serializers.DateTimeField()
    expected_version = serializers.IntegerField(required=True, min_value=1)


class SalonCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=500,
    )
    # Closed allowlist — the salon may assert what it genuinely knows and
    # nothing else. Omitted → the role default ("other").
    reason_code = serializers.ChoiceField(
        required=False,
        choices=sorted(SALON_CANCELLATION_REASON_CODES),
    )


class _SalonBookingBase(APIView):
    permission_classes = _SALON_WRITE_PERMISSIONS

    def _tenant(self, request):
        return getattr(request, "tenant", None)

    def _get_booking(self, request, appointment_id) -> Appointment | None:
        """Fetch a booking of the addressed tenant, or None.

        None must become 404 rather than 403 — the salon-admin surface
        does not confirm which appointment ids exist elsewhere.
        """
        tenant = self._tenant(request)
        if tenant is None:
            return None
        return (
            Appointment.objects
            .select_related("client", "specialist", "service")
            .prefetch_related("payments")
            .filter(id=appointment_id, tenant=tenant)
            .first()
        )

    @staticmethod
    def _not_found() -> Response:
        return error_response(
            "NOT_FOUND", "Appointment not found.", status_code=404,
        )


class SalonCustomerLookupView(APIView):
    """GET /api/v1/tenants/me/customers/?q=… — find a returning customer.

    Exists because the booking flow needs it: the Master Schedule UX
    Contract puts customer search *inside* the booking flow (there is no
    separate customers tab), and without a way to resolve a returning
    client to an id, the ``client_id`` path of the create endpoint is
    unreachable from a real console — the exact "the button has nowhere
    to point" failure this whole task exists to fix.

    Kept as narrow as it can be while still doing its job:

    * **Scoped to your own customers.** Only users with an active
      relationship with the addressed tenant. This is not a directory of
      Ayla users.
    * **The phone is an input, never an output.** You may look someone up
      by a number you already have; the response never contains one.
      Owner decision DRF-1039 is about Ayla not handing out customers'
      numbers, and a search endpoint is the classic way to leak them back.
    * **Minimum query length and a hard result cap**, so it answers
      "which of my customers is this?" and not "list my customers".
    """

    permission_classes = _SALON_WRITE_PERMISSIONS
    MIN_QUERY = 2
    LIMIT = 20

    @extend_schema(
        tags=["tenants"],
        responses={
            200: OpenApiResponse(description="Matching customers of this salon"),
            400: OpenApiResponse(description="Query too short"),
        },
    )
    def get(self, request: Request) -> Response:
        from django.db.models import Q

        from users.models import TenantUserRelationship

        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return error_response(
                "TENANT_REQUIRED", "Заголовок X-Tenant обязателен.",
                status_code=403,
            )

        query = (request.query_params.get("q") or "").strip()
        if len(query) < self.MIN_QUERY:
            return error_response(
                "VALIDATION_ERROR",
                f"Параметр q должен содержать минимум {self.MIN_QUERY} символа.",
                status_code=400,
            )

        # Phone match is exact — a prefix match on digits would turn this
        # into a way to sweep the customer list a few keystrokes at a time.
        from appointments.serializers import WalkInCreateSerializer
        normalised_phone = WalkInCreateSerializer().validate_client_phone(query)

        rows = (
            TenantUserRelationship.objects
            .filter(tenant=tenant, is_active=True)
            .filter(
                Q(user__first_name__istartswith=query)
                | Q(user__last_name__istartswith=query)
                | Q(user__phone=normalised_phone)
            )
            .select_related("user")
            .order_by("user__first_name", "user__last_name", "user_id")
            [:self.LIMIT]
        )

        from appointments.application.services.tenant_day_service import (
            _client_name,
        )
        results = [
            {"id": str(row.user_id), "name": _client_name(row.user)}
            for row in rows
        ]
        logger.info(
            "salon.customer_lookup tenant=%s by=%s hits=%d",
            tenant.slug, request.user.id, len(results),
        )
        return success_response({"results": results})


class SalonBookingCreateView(_SalonBookingBase):
    """POST /api/v1/tenants/me/appointments/ — book a customer in."""

    serializer_class = SalonBookingCreateSerializer

    @extend_schema(
        tags=["tenants"],
        request=SalonBookingCreateSerializer,
        responses={
            201: AppointmentDetailSerializer,
            400: OpenApiResponse(
                description=(
                    "Validation error, or X-Idempotency-Key header missing "
                    "(DRF-1232 — required so a retry cannot duplicate a booking)"
                ),
            ),
            403: OpenApiResponse(description="Not an administrator of this salon"),
            404: OpenApiResponse(description="Specialist or customer not in this salon"),
            409: OpenApiResponse(description="Slot taken"),
        },
    )
    def post(self, request: Request) -> Response:
        from users.models import SpecialistProfile, TenantUserRelationship
        from users.services import get_or_create_walkin_client

        tenant = self._tenant(request)
        serializer = SalonBookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # The master must be this salon's. Filtering by the resolved
        # tenant object — never a body value — means a master of another
        # salon is simply not found.
        specialist = (
            SpecialistProfile.objects
            .filter(id=data["specialist_id"], tenant=tenant)
            .first()
        )
        if specialist is None:
            return error_response(
                "NOT_FOUND", "Specialist not found.", status_code=404,
            )

        if data.get("client_id"):
            # An existing customer of THIS salon. A booking grants the
            # tenant a relationship with the customer, so accepting any
            # user id would let a salon attach itself to a stranger.
            known = TenantUserRelationship.objects.filter(
                user_id=data["client_id"], tenant=tenant, is_active=True,
            ).exists()
            if not known:
                return error_response(
                    "NOT_FOUND",
                    "Customer not found in this salon. Book them as a new "
                    "guest by name instead.",
                    status_code=404,
                )
            client_id = data["client_id"]
        else:
            # Same proxy-account helper the master's walk-in uses: a
            # returning guest keeps one identity, and a number belonging
            # to a REAL registered account is never co-opted (152-ФЗ —
            # see get_or_create_walkin_client). Deliberately NOT mirrored
            # into Appointment.notes the way walk-in does: there the
            # master typed the number themselves, here it would put a
            # number the salon collected in front of a master who never
            # saw it (DRF-1039).
            guest = get_or_create_walkin_client(
                data["client_name"], data.get("client_phone") or None,
            )
            client_id = guest.id

        # DRF-1232. The header is REQUIRED, and a missing one is a 400
        # rather than a generated uuid.
        #
        # ``Appointment.idempotency_key`` is a real, unique de-duplication
        # key: CreateBookingService looks the row up by it and returns the
        # existing appointment instead of making a second one. Inventing a
        # value per request kept that machinery running while guaranteeing
        # it could never match — every retry arrived with a key nothing had
        # ever been stored under, so the caller got a duplicate booking and
        # a 201 that looked like success.
        #
        # The failure mode needs a caller who repeats a request, which is
        # exactly what a caller does when a write times out and they cannot
        # tell whether it landed. That is the moment idempotency exists for.
        #
        # Note for the reader who checks the sibling endpoints: reschedule
        # and cancel pass ``command_key=... or None`` and look idempotent by
        # comparison. They are not — nothing ever queries by
        # ``command_key``; it is written to AppointmentRevision as an audit
        # trace. Reschedule is protected from repeats by ``expected_version``
        # instead. So this is not "make create behave like reschedule": create
        # is the only one of the three with key-based de-duplication, and the
        # only one where a missing key silently destroys it.
        key = (request.META.get("HTTP_X_IDEMPOTENCY_KEY") or "").strip()
        if not key:
            return error_response(
                "IDEMPOTENCY_KEY_REQUIRED",
                "X-Idempotency-Key header is required. Reuse the same value "
                "when retrying a booking, or a retry will create a second "
                "appointment.",
                status_code=400,
            )

        dto = CreateBookingDTO(
            client_id=client_id,
            specialist_id=specialist.id,
            service_id=data["service_id"],
            start_at=data["start_datetime"],
            idempotency_key=key,
            request_tenant_id=tenant.id,
            # Pilot baseline: the salon books, the customer pays at the
            # salon. No Payment row, straight to CONFIRMED — the same
            # shape as the master's walk-in path.
            payment_required=False,
            confirm_immediately=True,
            actor_role=OperationalActor.SALON.value,
        )

        try:
            result = CreateBookingService().execute(dto)
        except SlotNotAvailableError as exc:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(exc), status_code=409,
            )
        except BookingWindowError as exc:
            return error_response(
                "BOOKING_WINDOW_INVALID", str(exc), status_code=400,
            )

        appointment = (
            Appointment.objects
            .select_related("client", "specialist", "service")
            .prefetch_related("payments")
            .get(id=result.booking_id)
        )
        logger.info(
            "salon.booking_created appointment_id=%s tenant=%s by=%s",
            appointment.id, tenant.slug, request.user.id,
        )
        return success_response(
            AppointmentDetailSerializer(appointment).data, status_code=201,
        )


class SalonBookingRescheduleView(_SalonBookingBase):
    """POST /api/v1/tenants/me/appointments/{id}/reschedule/"""

    serializer_class = SalonRescheduleSerializer

    @extend_schema(
        tags=["tenants"],
        request=SalonRescheduleSerializer,
        responses={
            200: AppointmentDetailSerializer,
            404: OpenApiResponse(description="Not a booking of this salon"),
            409: OpenApiResponse(description="Stale version or slot taken"),
        },
    )
    def post(self, request: Request, appointment_id) -> Response:
        tenant = self._tenant(request)
        appointment = self._get_booking(request, appointment_id)
        if appointment is None:
            return self._not_found()

        serializer = SalonRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dto = RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            new_start_at=serializer.validated_data["new_start_datetime"],
            initiator_role=OperationalActor.SALON.value,
            expected_version=serializer.validated_data["expected_version"],
            tenant_id=tenant.id,
            command_key=request.META.get("HTTP_X_IDEMPOTENCY_KEY") or None,
            basis="salon_console",
        )

        try:
            result = RescheduleBookingService().execute(dto)
        except SlotNotAvailableError as exc:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(exc), status_code=409,
            )
        except StaleVersionError as exc:
            return error_response("STALE_VERSION", str(exc), status_code=409)
        except AppointmentTerminalError as exc:
            return error_response(
                "APPOINTMENT_TERMINAL", str(exc), status_code=409,
            )
        except RescheduleNotAllowedError as exc:
            return error_response(
                "RESCHEDULE_NOT_ALLOWED", str(exc), status_code=422,
            )
        except InvalidStateTransitionError as exc:
            return error_response("INVALID_STATUS", str(exc), status_code=422)
        except BookingWindowError as exc:
            return error_response(
                "BOOKING_WINDOW_INVALID", str(exc), status_code=400,
            )
        except TenantMismatchError:
            return self._not_found()

        appointment.refresh_from_db()
        payload = AppointmentDetailSerializer(appointment).data
        payload["revision_id"] = str(result.revision_id)
        logger.info(
            "salon.booking_rescheduled appointment_id=%s tenant=%s by=%s",
            appointment.id, tenant.slug, request.user.id,
        )
        return success_response(payload)


class SalonBookingCancelView(_SalonBookingBase):
    """POST /api/v1/tenants/me/appointments/{id}/cancel/"""

    serializer_class = SalonCancelSerializer

    @extend_schema(
        tags=["tenants"],
        request=SalonCancelSerializer,
        responses={
            200: AppointmentDetailSerializer,
            404: OpenApiResponse(description="Not a booking of this salon"),
            422: OpenApiResponse(description="Not cancellable from this state"),
        },
    )
    def post(self, request: Request, appointment_id) -> Response:
        tenant = self._tenant(request)
        appointment = self._get_booking(request, appointment_id)
        if appointment is None:
            return self._not_found()

        serializer = SalonCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dto = CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            initiator_role=OperationalActor.SALON.value,
            reason=serializer.validated_data.get("reason", ""),
            # Already validated against the salon allowlist by the
            # ChoiceField above — this is the "trusted" channel the
            # service distinguishes from client free-text.
            reason_code=serializer.validated_data.get("reason_code"),
        )

        try:
            CancelBookingService().execute(dto)
        except CancellationNotAllowedError as exc:
            return error_response(
                "CANCELLATION_NOT_ALLOWED", str(exc), status_code=422,
            )
        except InvalidStateTransitionError as exc:
            return error_response("INVALID_STATUS", str(exc), status_code=422)

        appointment.refresh_from_db()
        logger.info(
            "salon.booking_cancelled appointment_id=%s tenant=%s by=%s",
            appointment.id, tenant.slug, request.user.id,
        )
        return success_response(
            AppointmentDetailSerializer(appointment).data,
        )
