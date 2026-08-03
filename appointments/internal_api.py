"""Internal Bearer booking writes — create / cancel / reschedule (#1016 S2).

Service-to-service surface the nationwide Ayla bot calls to mutate a
booking on behalf of a verified end-user. Ayla backend remains the
single source of truth (ADR-0009); the bot owns no booking state, it
only drives these REST wrappers.

Auth: ``IsBotServiceWithVerifiedClient`` — ``Authorization: Bearer
<AYLA_INTERNAL_API_TOKEN>`` + ``X-External-User-ID: <source>:<id>``.
The permission resolves the external id to an Ayla ``User`` and
overwrites ``request.user`` so the same per-user filters as the mobile
path apply. On create the body MUST also echo ``client_id ==
request.user.id`` (defense-in-depth: a leaked bearer alone cannot
impersonate an arbitrary user — see InternalPaymentRetryView / #85).

All three reuse the booking engine application services unchanged, so
idempotency, ``select_for_update`` conflict re-check, snapshots, the
grant-on-first-booking rule, and ADR-0009 outbox emit are identical to
the mobile path. Booking domain errors propagate to
``api_exception_handler`` which maps them to the canonical envelopes
(SLOT_NOT_AVAILABLE → 409, etc.); the grant F2 ``NotFound`` → 404.
"""
from __future__ import annotations

import logging
from uuid import UUID, uuid4

from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response

from .application.dto import (
    CancelBookingDTO,
    CreateBookingDTO,
    RescheduleBookingDTO,
)
from .application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from .application.services.create_booking_service import CreateBookingService
from .domain.exceptions import (
    AppointmentTerminalError,
    BookingWindowError,
    CancellationNotAllowedError,
    ExpectedVersionRequiredError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from .infrastructure.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
    lookup_or_open_idempotency,
    record_response,
)
from .models import Appointment
from .serializers import (
    AppointmentCancelSerializer,
    AppointmentDetailSerializer,
    InternalAppointmentRescheduleSerializer,
)

logger = logging.getLogger(__name__)


def _idempotency_key_from(request: Request) -> str:
    """X-Idempotency-Key header (agreed with S1), fallback to a fresh
    UUID so a header-less caller still gets a single-shot create.

    ``InternalBookingCreateView`` ONLY. This fallback feeds
    ``CreateBookingDTO.idempotency_key`` — a per-call value that, when
    server-generated, guarantees nothing across retries (a fresh UUID
    each time means CreateBookingService still runs unconditionally
    once per HTTP call). It exists purely so create keeps working for
    a header-less legacy caller; it is NOT a dedup mechanism.

    Reschedule and cancel do NOT use this function or any equivalent
    fallback — they call ``_require_idempotency_key`` instead, which
    REJECTS a header-less request outright (see below). New bot
    integrations MUST always send a real X-Idempotency-Key on every
    mutating internal call; do not treat this fallback as something to
    rely on or replicate for new endpoints.
    """
    return request.META.get('HTTP_X_IDEMPOTENCY_KEY') or str(uuid4())


def _require_idempotency_key(request: Request) -> str | None:
    """X-Idempotency-Key for internal Reschedule/Cancel — MANDATORY,
    unlike ``_idempotency_key_from`` above (create-only, has a
    server-generated fallback). Returns the header value, or ``None``
    if it is missing or blank (an empty string is treated the same as
    absent — a caller that sends the header but leaves it empty gets
    the same rejection, not a silently-accepted empty key).

    The bot previously got a "soft" contract here (header recommended,
    but a header-less call still executed with zero dedup protection —
    see the Wave 1 targeted-patch history in this module's tests). That
    gap is closed: callers of this function MUST short-circuit BEFORE
    touching the application service, so a rejected request creates no
    mutation, no ``AppointmentRevision``, and no outbox event.
    """
    key = request.META.get('HTTP_X_IDEMPOTENCY_KEY')
    return key or None


class InternalBookingCreateSerializer(serializers.Serializer):
    """Validation-only — booking logic stays in CreateBookingService."""
    client_id = serializers.UUIDField()
    specialist_id = serializers.UUIDField()
    service_id = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()
    # D6 — online payment is OPTIONAL. Default True preserves the
    # pre-pilot contract (AWAITING_PAYMENT + pending Payment). The bot
    # passes False for the pilot baseline "запись без предоплаты":
    # no Payment row, booking lands directly in CONFIRMED and
    # booking.confirmed is emitted (R1). Additive contract change
    # (#1016, MINOR) — omitted field behaves exactly as before.
    payment_required = serializers.BooleanField(required=False, default=True)


class _InternalAuthMixin:
    """Bot bearer is not a JWT, so disable DRF's JWTAuthentication (it
    would 401 before the permission runs). The permission class is the
    sole auth boundary — same pattern as InternalPaymentRetryView."""
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]


class InternalBookingCreateView(_InternalAuthMixin, APIView):
    """POST /api/v1/internal/appointments/ — create on behalf of the
    resolved customer. Honours X-Idempotency-Key."""
    serializer_class = InternalBookingCreateSerializer

    @extend_schema(
        operation_id="internal_appointments_create",
        tags=["internal"],
        request=InternalBookingCreateSerializer,
        responses={
            201: AppointmentDetailSerializer,
            403: OpenApiResponse(
                description="body.client_id does not match resolved actor",
            ),
            404: OpenApiResponse(description="Specialist not found / revoked"),
            409: OpenApiResponse(description="Slot not available"),
            422: OpenApiResponse(description="Specialist/service not bookable"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = InternalBookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Defense-in-depth: the body must independently name the same
        # Ayla user-id the bearer + X-External-User-ID resolved to.
        claimed = serializer.validated_data['client_id']
        if str(claimed) != str(request.user.id):
            logger.warning(
                'internal.booking.create client_mismatch resolved=%s body=%s',
                request.user.id, claimed,
            )
            return error_response(
                'CLIENT_MISMATCH',
                'body.client_id does not match resolved actor.',
                status_code=403,
            )

        dto = CreateBookingDTO(
            client_id=request.user.id,
            specialist_id=serializer.validated_data['specialist_id'],
            service_id=serializer.validated_data['service_id'],
            start_at=serializer.validated_data['start_datetime'],
            idempotency_key=_idempotency_key_from(request),
            # Bot context has no client tenant scope; the grant keys off
            # the specialist's tenant regardless (#1014).
            request_tenant_id=None,
            # D6: payment_required=False → confirm immediately without a
            # Payment row (no-prepayment pilot baseline).
            payment_required=serializer.validated_data['payment_required'],
            confirm_immediately=(
                not serializer.validated_data['payment_required']
            ),
        )

        # Booking domain errors (slot taken, inactive specialist/service)
        # and the grant F2 NotFound propagate to api_exception_handler.
        from appointments.domain.exceptions import BillingEligibilityError
        try:
            result = CreateBookingService().execute(dto)
        except BillingEligibilityError as exc:
            # C1: the INTERNAL/backend surface gets the real reason code
            # (the bot routes the master to the debt screen; the customer
            # gets a neutral message bot-side).
            return error_response(
                exc.reason,
                'Subscription payment is past due — new bookings are '
                'blocked for this specialist.',
                status_code=409,
            )

        appointment = (
            Appointment.objects
            .select_related('client', 'specialist', 'service')
            .prefetch_related('payments')
            .get(id=result.booking_id)
        )
        return success_response(
            AppointmentDetailSerializer(appointment).data,
            status_code=201,
        )


class InternalBookingCancelView(_InternalAuthMixin, APIView):
    """POST /api/v1/internal/appointments/{booking_id}/cancel/ — cancel
    one of the resolved customer's bookings."""
    serializer_class = AppointmentCancelSerializer

    @extend_schema(
        operation_id="internal_appointments_cancel",
        tags=["internal"],
        request=AppointmentCancelSerializer,
        parameters=[
            OpenApiParameter(
                name="X-Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description=(
                    "REQUIRED. No server-generated fallback exists on "
                    "this path (unlike the create endpoint). Missing or "
                    "empty -> 400 IDEMPOTENCY_KEY_REQUIRED, no mutation."
                ),
            ),
        ],
        description=(
            "X-Idempotency-Key is REQUIRED for this endpoint (enforced "
            "server-side, not just recommended). A missing or empty "
            "header is rejected with 400 IDEMPOTENCY_KEY_REQUIRED "
            "before the application service runs — no mutation, "
            "Revision, or outbox event is created. New bot integrations "
            "must always send a stable key; there is no legacy fallback "
            "here (contrast InternalBookingCreateView)."
        ),
        responses={
            200: AppointmentDetailSerializer,
            400: OpenApiResponse(
                description="X-Idempotency-Key missing/empty (IDEMPOTENCY_KEY_REQUIRED)",
            ),
            404: OpenApiResponse(description="Booking not found for this user"),
            422: OpenApiResponse(description="Cancellation not allowed"),
        },
    )
    def post(self, request: Request, booking_id: UUID) -> Response:
        # X-Idempotency-Key is MANDATORY on this path (targeted patch —
        # AGENT_BE_ENFORCE_INTERNAL_IDEMPOTENCY_BEFORE_COMMIT.md item 1).
        # Reject BEFORE touching lookup_or_open_idempotency / the
        # application service: a header-less or blank-header call must
        # produce zero side effects (no mutation, no Revision, no
        # outbox event), not an undeduped mutation as before.
        if not _require_idempotency_key(request):
            return error_response(
                "IDEMPOTENCY_KEY_REQUIRED",
                "X-Idempotency-Key is required for internal cancel.",
                status_code=400,
            )

        # Same outer/_inner pattern as the mobile view; a distinct
        # operation_name ("...internal") keeps the bot's key namespace
        # separate from the mobile app's, so a coincidentally identical
        # key value from a different channel can't collide.
        try:
            cached, idem_record = lookup_or_open_idempotency(
                request,
                operation_name="booking.cancel.internal",
                target_type="Appointment",
                target_id=str(booking_id),
            )
        except IdempotencyConflict as exc:
            return error_response(
                "IDEMPOTENCY_CONFLICT", str(exc), status_code=422,
            )
        except IdempotencyInFlight as exc:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT", str(exc), status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = self._cancel_inner(request, booking_id)
        if idem_record is not None:
            record_response(idem_record, response.status_code, response.data)
        return response

    def _cancel_inner(self, request: Request, booking_id: UUID) -> Response:
        # Owner-scoped fetch: the booking_id owned by the resolved user
        # is the second factor — a leaked bearer cannot cancel a booking
        # it cannot also name under the correct customer. Info-hidden 404.
        try:
            appointment = Appointment.objects.filter(
                client=request.user,
            ).get(pk=booking_id)
        except Appointment.DoesNotExist:
            return error_response(
                'NOT_FOUND', 'Запись не найдена.', status_code=404,
            )

        serializer = AppointmentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            CancelBookingService().execute(CancelBookingDTO(
                booking_id=appointment.id,
                initiator_user_id=request.user.id,
                initiator_role="client",
                reason=serializer.validated_data.get('reason', ''),
            ))
        except CancellationNotAllowedError as e:
            return error_response(
                "CANCELLATION_NOT_ALLOWED", str(e), status_code=422,
            )
        except InvalidStateTransitionError as e:
            return error_response(
                "INVALID_STATUS", str(e), status_code=422,
            )
        appointment.refresh_from_db()
        return success_response(AppointmentDetailSerializer(appointment).data)


class InternalBookingRescheduleView(_InternalAuthMixin, APIView):
    """POST /api/v1/internal/appointments/{booking_id}/reschedule/ —
    move one of the resolved customer's bookings to a new slot.

    Unlike the mobile action, ``expected_version`` is REQUIRED here
    (see ``InternalAppointmentRescheduleSerializer`` — Wave 1 bot
    contract, owner decision)."""
    serializer_class = InternalAppointmentRescheduleSerializer

    @extend_schema(
        operation_id="internal_appointments_reschedule",
        tags=["internal"],
        request=InternalAppointmentRescheduleSerializer,
        parameters=[
            OpenApiParameter(
                name="X-Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
                description=(
                    "REQUIRED. No server-generated fallback exists on "
                    "this path (unlike the create endpoint). Missing or "
                    "empty -> 400 IDEMPOTENCY_KEY_REQUIRED, no mutation."
                ),
            ),
        ],
        description=(
            "X-Idempotency-Key is REQUIRED for this endpoint (enforced "
            "server-side, not just recommended). A missing or empty "
            "header is rejected with 400 IDEMPOTENCY_KEY_REQUIRED "
            "before the application service runs — no mutation, "
            "Revision, or outbox event is created. expected_version is "
            "ALSO required and is an independent safety net (see "
            "StaleVersionError, 409) but does not substitute for the "
            "idempotency key: without the key, a legitimate retry after "
            "a lost response gets rejected/conflicts instead of "
            "replaying the original success. New bot integrations must "
            "always send a stable key; there is no legacy fallback here "
            "(contrast InternalBookingCreateView)."
        ),
        responses={
            200: AppointmentDetailSerializer,
            400: OpenApiResponse(
                description=(
                    "expected_version missing/invalid, or "
                    "X-Idempotency-Key missing/empty (IDEMPOTENCY_KEY_REQUIRED)"
                ),
            ),
            404: OpenApiResponse(description="Booking not found for this user"),
            409: OpenApiResponse(
                description=(
                    "New slot not available / stale version / "
                    "appointment terminal"
                ),
            ),
            422: OpenApiResponse(description="Reschedule not allowed"),
        },
    )
    def post(self, request: Request, booking_id: UUID) -> Response:
        # X-Idempotency-Key is MANDATORY on this path (targeted patch —
        # AGENT_BE_ENFORCE_INTERNAL_IDEMPOTENCY_BEFORE_COMMIT.md item 1).
        # Reject BEFORE touching lookup_or_open_idempotency / the
        # application service: a header-less or blank-header call must
        # produce zero side effects (no mutation, no Revision, no
        # outbox event) — expected_version alone is NOT a substitute:
        # it stops a *silent double reschedule* (stale retry -> 409) but
        # does not give a legitimate retry back its original response,
        # which is the whole point of the key.
        if not _require_idempotency_key(request):
            return error_response(
                "IDEMPOTENCY_KEY_REQUIRED",
                "X-Idempotency-Key is required for internal reschedule.",
                status_code=400,
            )

        try:
            cached, idem_record = lookup_or_open_idempotency(
                request,
                operation_name="booking.reschedule.internal",
                target_type="Appointment",
                target_id=str(booking_id),
            )
        except IdempotencyConflict as exc:
            return error_response(
                "IDEMPOTENCY_CONFLICT", str(exc), status_code=422,
            )
        except IdempotencyInFlight as exc:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT", str(exc), status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = self._reschedule_inner(request, booking_id)
        if idem_record is not None:
            record_response(idem_record, response.status_code, response.data)
        return response

    def _reschedule_inner(self, request: Request, booking_id: UUID) -> Response:
        try:
            appointment = Appointment.objects.filter(
                client=request.user,
            ).get(pk=booking_id)
        except Appointment.DoesNotExist:
            return error_response(
                'NOT_FOUND', 'Запись не найдена.', status_code=404,
            )

        serializer = InternalAppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appointment.id,
                initiator_user_id=request.user.id,
                new_start_at=serializer.validated_data['new_start_datetime'],
                initiator_role="client",
                expected_version=serializer.validated_data['expected_version'],
                # Bot context has no client tenant scope (same rationale
                # as CreateBookingDTO.request_tenant_id above).
                tenant_id=None,
                command_key=request.META.get('HTTP_X_IDEMPOTENCY_KEY') or None,
                basis="internal_bot",
            ))
        except SlotNotAvailableError as e:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(e), status_code=409,
            )
        except RescheduleNotAllowedError as e:
            return error_response(
                "RESCHEDULE_NOT_ALLOWED", str(e), status_code=422,
            )
        except BookingWindowError as e:
            return error_response(
                "BOOKING_WINDOW_INVALID", str(e), status_code=400,
            )
        except StaleVersionError as e:
            return error_response(
                "STALE_VERSION", str(e), status_code=409,
            )
        except AppointmentTerminalError as e:
            return error_response(
                "APPOINTMENT_TERMINAL", str(e), status_code=409,
            )
        except ExpectedVersionRequiredError as e:
            # Defensive only — InternalAppointmentRescheduleSerializer
            # already makes expected_version required=True, so this
            # path isn't reachable through this view today.
            return error_response(
                "EXPECTED_VERSION_REQUIRED", str(e), status_code=400,
            )
        except TenantMismatchError:
            return error_response(
                'NOT_FOUND', 'Запись не найдена.', status_code=404,
            )
        appointment.refresh_from_db()
        data = AppointmentDetailSerializer(appointment).data
        # revision_id — see the mobile view's identical comment
        # (AppointmentViewSet._reschedule_inner in views.py).
        data['revision_id'] = str(result.revision_id)
        return success_response(data)
