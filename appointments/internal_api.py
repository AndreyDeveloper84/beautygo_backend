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

from drf_spectacular.utils import OpenApiResponse, extend_schema
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
from .models import Appointment
from .serializers import (
    AppointmentCancelSerializer,
    AppointmentDetailSerializer,
    AppointmentRescheduleSerializer,
)

logger = logging.getLogger(__name__)


def _idempotency_key_from(request: Request) -> str:
    """X-Idempotency-Key header (agreed with S1), fallback to a fresh
    UUID so a header-less caller still gets a single-shot create."""
    return request.META.get('HTTP_X_IDEMPOTENCY_KEY') or str(uuid4())


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
        result = CreateBookingService().execute(dto)

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
        responses={
            200: AppointmentDetailSerializer,
            404: OpenApiResponse(description="Booking not found for this user"),
            422: OpenApiResponse(description="Cancellation not allowed"),
        },
    )
    def post(self, request: Request, booking_id: UUID) -> Response:
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

        CancelBookingService().execute(CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            initiator_role="client",
            reason=serializer.validated_data.get('reason', ''),
        ))
        appointment.refresh_from_db()
        return success_response(AppointmentDetailSerializer(appointment).data)


class InternalBookingRescheduleView(_InternalAuthMixin, APIView):
    """POST /api/v1/internal/appointments/{booking_id}/reschedule/ —
    move one of the resolved customer's bookings to a new slot."""
    serializer_class = AppointmentRescheduleSerializer

    @extend_schema(
        operation_id="internal_appointments_reschedule",
        tags=["internal"],
        request=AppointmentRescheduleSerializer,
        responses={
            200: AppointmentDetailSerializer,
            404: OpenApiResponse(description="Booking not found for this user"),
            409: OpenApiResponse(description="New slot not available"),
            422: OpenApiResponse(description="Reschedule not allowed"),
        },
    )
    def post(self, request: Request, booking_id: UUID) -> Response:
        try:
            appointment = Appointment.objects.filter(
                client=request.user,
            ).get(pk=booking_id)
        except Appointment.DoesNotExist:
            return error_response(
                'NOT_FOUND', 'Запись не найдена.', status_code=404,
            )

        serializer = AppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            new_start_at=serializer.validated_data['new_start_datetime'],
            initiator_role="client",
        ))
        appointment.refresh_from_db()
        return success_response(AppointmentDetailSerializer(appointment).data)
