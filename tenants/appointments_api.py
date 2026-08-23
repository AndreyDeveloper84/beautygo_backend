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
from rest_framework import serializers
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
from users.permissions import IsBotServiceWithVerifiedClient, IsTenantAdmin
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

# Writes on this surface stamp WHO acted onto the booking and its events.
# `IsTenantAdmin` rather than DRF-1062's `IsTenantAdminOrPlatformAdmin`:
# there is no canonical actor value for Ayla platform staff operating
# inside a tenant, and recording their action as `salon` would be false.
# Schedule edits (DRF-1062) are unattributed configuration, so the wider
# permission is right there and not here. Raised as an owner question.
#
# DRF-1231 — the caller is the MAX bot, not a mobile app.
#
# The old list could not be satisfied by anything: the surface's only
# client is the bot, which authenticates with a service Bearer, and the
# JWT authenticator installed by DEFAULT_AUTHENTICATION_CLASSES rejected
# that credential with 401 *before* any permission ran. No permission
# class can rescue a request the authenticator already refused, which is
# why every existing test — all of them using force_authenticate, which
# skips that layer — stayed green while the live endpoint was unreachable.
#
# So `authentication_classes` is emptied on the views below and the
# permission classes become the sole authority. Two of them, and both
# earn their place:
#
#   IsBotServiceWithVerifiedClient — proves the CALL comes from the bot
#     (constant-time Bearer) and resolves X-External-User-ID into the
#     acting Ayla user, so `request.user` is the administrator who
#     pressed the button. Attribution downstream (initiator_user_id,
#     `salon.booking_*` logs) keeps working unchanged.
#   IsTenantAdmin — proves that PERSON may act in THIS salon.
#
# Dropping the second would be the whole security story: the bearer is a
# single shared secret, so a leak would otherwise let its holder write
# into any tenant by naming one in X-Tenant. It is also what makes
# `permissions.IsAuthenticated` redundant here (it re-checks
# `is_authenticated` itself) — and IsAuthenticated could not have stayed
# first in the list anyway, since `request.user` is still anonymous until
# IsBotServiceWithVerifiedClient resolves it.
#
# Note the defense-in-depth rule in that permission's docstring — "the
# view MUST cross-check the body's client_id against request.user.id" —
# does NOT apply on this surface and must not be copied here: the actor
# is the administrator and `client_id` names the CUSTOMER being booked,
# deliberately a different person. IsTenantAdmin is this surface's
# equivalent second factor.
#
# Attribution rationale (unchanged, pre-dates DRF-1231): `IsTenantAdmin`
# rather than DRF-1062's `IsTenantAdminOrPlatformAdmin` — there is no
# canonical actor value for Ayla platform staff operating inside a
# tenant, and recording their action as `salon` would be false. Schedule
# edits (DRF-1062) are unattributed configuration, so the wider
# permission is right there and not here. Raised as an owner question.
_SALON_WRITE_PERMISSIONS = [
    IsBotServiceWithVerifiedClient,
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
    # Empty, not "one more authenticator": DEFAULT_AUTHENTICATION_CLASSES
    # runs the JWT authenticator, which raises 401 on a service Bearer
    # before permissions are consulted. See _SALON_WRITE_PERMISSIONS.
    authentication_classes: list = []
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

    def _assert_authority(self, request, appointment) -> Response | None:
        """In what capacity does this caller act on THIS row? None -> 404.

        DRF-1297 B-2. Read this as consistency, not as a hole being
        plugged: today the answer can only be ``salon``, because
        ``IsTenantAdmin`` has already proved the grant in
        ``request.tenant`` and ``_get_booking`` has already refused any
        row outside it -- which is exactly the pair of conditions
        ``resolve_booking_operator`` tests. The check cannot currently
        fail, and that is the point of stating it.

        What it buys is that all four salon operations now derive
        authority from the same module in the same way. ``complete``
        already did (see ``SalonBookingCompleteView``); ``reschedule``
        and ``cancel`` inferred it from the composition of a permission
        class and a queryset filter, which is correct but is not written
        down anywhere a reader -- or a future change to either half --
        would meet it.

        The resolved capacity is deliberately NOT reused as the DTO's
        ``initiator_role``. That value picks the cancellation-refund
        policy, and a master who also holds an admin grant resolves as
        ``specialist`` on their own row: feeding it through would quietly
        reprice a front-desk cancellation. The salon surface asserts
        ``salon`` because that is who pressed the button.
        """
        from appointments.authz import resolve_booking_operator

        if resolve_booking_operator(request, appointment) is None:
            return self._not_found()
        return None


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

    authentication_classes: list = []  # DRF-1231 — see _SalonBookingBase.
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

        denied = self._assert_authority(request, appointment)
        if denied is not None:
            return denied

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

        denied = self._assert_authority(request, appointment)
        if denied is not None:
            return denied

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


class SalonCompleteSerializer(serializers.Serializer):
    """``expected_version`` is REQUIRED here, as on salon reschedule.

    Optional on the mobile path because app builds predating the field
    exist; the salon console has no such legacy. It reads the booking —
    and its ``version`` — immediately before offering the button, which
    is exactly the flow ``AppointmentCompleteSerializer`` describes for
    «new surfaces» and which only became possible once the bot had a
    canonical read (DRF-1233).

    Closure does not bump ``version`` — that counter tracks reschedules —
    so this is a guard against closing a booking that moved under you,
    not a lock on closure itself.
    """

    expected_version = serializers.IntegerField(required=True, min_value=1)


class SalonBookingCompleteView(_SalonBookingBase):
    """POST /api/v1/tenants/me/appointments/{id}/complete/ — close a visit.

    A thin wrapper, not a second implementation. ``POST
    /api/v1/appointments/{id}/complete/`` (DRF-1064) already does this and
    already accepts a salon administrator — but it lives on the mobile
    client path, which the bot cannot reach: no ``X-App-Type``, a JWT
    authenticator that refuses a service Bearer, and permissions built
    for a logged-in person. Exempting THAT prefix was never an option —
    an exclusion does not relax the app-type requirement, it sets
    ``request.app_type = None`` and makes ``IsProApp`` unsatisfiable
    permanently, and the blast radius there is the whole mobile app.

    So the same steps run here, on a surface the bot already reaches:
    lock, capacity, tenant assertion, version, ``close_booking``,
    capture. The domain lives in
    ``appointments.application.services.completion``, shared verbatim
    with the mobile view and the three-hour sweep — a visit closed at the
    front desk and one closed by the sweep stay indistinguishable to
    every consumer, which is the property that would have been lost by
    copying the logic instead of calling it.

    ``no_show`` is deliberately NOT here. Its mobile implementation
    shapes two outbox events inline (including the fact that the ingest
    taxonomy has no ``booking.no_show``, so it travels as
    ``booking.cancelled`` + ``reason_code``), and duplicating sixty lines
    of event construction creates two paths that must agree forever.
    Extracting it into a sibling of ``close_booking`` is its own task,
    because it touches the live mobile path.
    """

    serializer_class = SalonCompleteSerializer

    @extend_schema(
        tags=["tenants"],
        request=SalonCompleteSerializer,
        responses={
            200: AppointmentDetailSerializer,
            404: OpenApiResponse(description="Not a booking of this salon"),
            409: OpenApiResponse(description="Stale version"),
            422: OpenApiResponse(description="Not completable from this state"),
        },
    )
    def post(self, request: Request, appointment_id) -> Response:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.db import transaction

        from appointments.application.services.completion import (
            close_booking,
            schedule_capture_safely,
        )
        from appointments.authz import resolve_booking_operator

        tenant = self._tenant(request)
        if tenant is None:
            return self._not_found()

        serializer = SalonCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expected_version = serializer.validated_data["expected_version"]

        try:
            with transaction.atomic():
                # Locked inside the transaction so two concurrent closures
                # serialise here. Without it both would see CONFIRMED,
                # both would flip the status and both would emit — and
                # bot-platform would process two completions of one visit.
                # of=("self",): AMD-019 made `service` nullable, so
                # select_related emits an outer join and bare FOR UPDATE
                # is rejected by Postgres.
                appointment = (
                    Appointment.objects
                    .select_for_update(of=("self",))
                    .select_related("specialist", "client", "service")
                    .filter(tenant=tenant)
                    .filter(pk=appointment_id)
                    .first()
                )
                if appointment is None:
                    return self._not_found()

                # In what capacity does this caller act on THIS row —
                # reused from the mobile path rather than re-derived, so
                # the two surfaces can never disagree about who the actor
                # is. None → 404: «forbidden» would confirm the id exists.
                actor = resolve_booking_operator(request, appointment)
                if actor is None:
                    return self._not_found()

                if appointment.version != expected_version:
                    return error_response(
                        "STALE_VERSION",
                        f"Appointment {appointment.id} expected_version="
                        f"{expected_version} but current version is "
                        f"{appointment.version}.",
                        status_code=409,
                    )

                close_booking(appointment, completed_by=actor)
        except DjangoValidationError as exc:
            # A visit that is cancelled, or already closed. Settled, not
            # contended: no retry and no other actor changes the answer.
            return error_response(
                "INVALID_STATUS", str(exc.message), status_code=422,
            )

        # Outside the atomic block on purpose: the closure is durable by
        # now, and a broker hiccup must not surface as the failure of a
        # fact that already happened. Reconciliation covers the rest.
        schedule_capture_safely(appointment)

        logger.info(
            "salon.booking_completed appointment_id=%s tenant=%s by=%s actor=%s",
            appointment.id, tenant.slug, request.user.id, actor,
        )
        return success_response(AppointmentDetailSerializer(appointment).data)
