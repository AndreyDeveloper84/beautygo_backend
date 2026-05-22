"""Appointment views — thin views delegating to application services."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

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
    CancellationNotAllowedError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
)
from .infrastructure.outbox import emit_outbox_event, safe_tenant_id
from .models import Appointment, OutboxEvent
from .serializers import (
    AppointmentCancelSerializer,
    AppointmentCreateSerializer,
    AppointmentDetailSerializer,
    AppointmentListSerializer,
    AppointmentRescheduleSerializer,
)

logger = logging.getLogger(__name__)


class AppointmentViewSet(viewsets.GenericViewSet):
    """
    Appointments API.

    - POST   /api/v1/appointments/              Create (client only)
    - GET    /api/v1/appointments/              My appointments list
    - GET    /api/v1/appointments/{id}/         Detail
    - POST   /api/v1/appointments/{id}/cancel/  Cancel
    - POST   /api/v1/appointments/{id}/complete/ Complete (specialist only)
    - POST   /api/v1/appointments/{id}/reschedule/ Reschedule
    """
    permission_classes = [permissions.IsAuthenticated]

    # Service classes — override in tests for mocking
    create_booking_service_class = CreateBookingService
    cancel_booking_service_class = CancelBookingService
    reschedule_booking_service_class = RescheduleBookingService

    queryset = Appointment.objects.none()

    def get_queryset(self) -> QuerySet:
        # During drf-spectacular schema generation the viewset is invoked
        # without a real user. Returning an empty queryset early skips
        # the role-based filter (which would raise AttributeError on
        # AnonymousUser.is_client) and lets spectacular derive the
        # model type from the queryset attribute alone.
        if getattr(self, 'swagger_fake_view', False):
            return Appointment.objects.none()
        user = self.request.user
        qs = (
            Appointment.objects
            .select_related('client', 'specialist', 'service', 'service__category')
            .prefetch_related('payments')
        )
        if user.is_client:
            return qs.filter(client=user)
        if user.is_specialist:
            return qs.filter(specialist__user=user)
        return qs.none()

    def get_serializer_class(self):
        # Per-action serializer mapping. drf-spectacular uses this to
        # derive request/response shapes; without it the viewset has
        # no canonical ``serializer_class`` (because each action uses
        # a different serializer) and schema generation falls back
        # to error mode.
        if self.action == 'list':
            return AppointmentListSerializer
        if self.action == 'create':
            return AppointmentCreateSerializer
        if self.action == 'cancel':
            return AppointmentCancelSerializer
        if self.action == 'reschedule':
            return AppointmentRescheduleSerializer
        # retrieve, complete, update_status — full detail body
        return AppointmentDetailSerializer

    # -- List ----------------------------------------------------------------

    @extend_schema(
        operation_id="appointments_list",
        responses={200: AppointmentListSerializer(many=True)},
    )
    def list(self, request: Request) -> Response:
        from core.pagination import paginated_success_response

        qs = self.get_queryset()
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return paginated_success_response(
            qs, AppointmentListSerializer, request,
        )

    # -- Create (via booking engine) ----------------------------------------

    @extend_schema(
        operation_id="appointments_create",
        request=AppointmentCreateSerializer,
        responses={201: AppointmentDetailSerializer},
    )
    def create(self, request: Request) -> Response:
        if not request.user.is_client:
            return error_response(
                "FORBIDDEN", "Only clients can create appointments.",
                status_code=403,
            )

        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        idempotency_key = request.META.get(
            'HTTP_X_IDEMPOTENCY_KEY', str(uuid4()),
        )

        dto = CreateBookingDTO(
            client_id=request.user.id,
            specialist_id=serializer.validated_data['specialist_id'],
            service_id=serializer.validated_data['service_id'],
            start_at=serializer.validated_data['start_datetime'],
            idempotency_key=idempotency_key,
        )

        service = self.create_booking_service_class()
        result = service.execute(dto)

        # Reload for full serialization
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

    # -- Retrieve ------------------------------------------------------------

    @extend_schema(
        operation_id="appointments_retrieve",
        responses={200: AppointmentDetailSerializer, 404: None},
    )
    def retrieve(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Cancel (via booking engine) ----------------------------------------

    @extend_schema(
        request=AppointmentCancelSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dto = CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            initiator_role="specialist" if request.user.is_specialist else "client",
            reason=serializer.validated_data.get('reason', ''),
        )

        try:
            self.cancel_booking_service_class().execute(dto)
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

    # -- Complete ------------------------------------------------------------

    @extend_schema(
        request=None,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def complete(self, request: Request, pk: Any = None) -> Response:
        if not request.user.is_specialist:
            return error_response(
                "FORBIDDEN",
                "Only specialists can mark appointments as complete.",
                status_code=403,
            )
        try:
            with transaction.atomic():
                # Fetch inside the atomic block with select_for_update so
                # two concurrent POSTs serialise here. Without the lock,
                # both would pass can_complete() (status still CONFIRMED
                # in both reads), both flip status, both emit with
                # distinct event_ids — bot-platform processes both,
                # double-completing the booking (adversarial review B3).
                #
                # We bypass get_queryset() (which filters by
                # specialist__user) because select_for_update is on the
                # raw Appointment manager. The explicit owner check on
                # the next line restores the scope guard — belt-and-
                # suspenders for B4.
                try:
                    appointment = (
                        Appointment.objects
                        .select_for_update()
                        .select_related('specialist', 'client', 'service')
                        .get(pk=pk)
                    )
                except Appointment.DoesNotExist:
                    return error_response(
                        "NOT_FOUND", "Appointment not found.", status_code=404,
                    )
                if appointment.specialist.user_id != request.user.id:
                    # Defence-in-depth: a specialist cannot complete a
                    # booking that isn't theirs even if get_queryset's
                    # filter is later refactored. Distinct from the
                    # is_specialist check above — that gates the role;
                    # this gates the row.
                    return error_response(
                        "NOT_FOUND",  # don't leak existence to other specialists
                        "Appointment not found.",
                        status_code=404,
                    )

                # complete() raises ValidationError if status not
                # CONFIRMED. Under the row lock that re-check sees the
                # committed status of any racing transaction.
                appointment.complete()
                emit_outbox_event(
                    topic=OutboxEvent.Topic.BOOKING_COMPLETED,
                    data={
                        "booking_id": str(appointment.id),
                        "client_id": str(appointment.client_id),
                        "specialist_id": str(appointment.specialist_id),
                    },
                    user_id=request.user.id,
                    tenant_id=safe_tenant_id(
                        appointment, context="booking.completed",
                    ),
                    # Specialist-initiated; per ADR-0009 actor mapping
                    # (specialist → 'admin', client → 'user').
                    actor="admin",
                )
        except DjangoValidationError as e:
            return error_response(
                "INVALID_STATUS", str(e.message), status_code=422,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Status (spec-compliant: PATCH /appointments/{id}/status/) ----------

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["cancelled", "completed"]}},
                "required": ["status"],
            },
        },
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['patch'], url_path='status')
    def update_status(self, request: Request, pk: Any = None) -> Response:
        """
        PATCH /appointments/{id}/status/ — spec-compliant status update.
        Delegates to cancel or complete based on requested status.
        """
        status_value = request.data.get('status')
        if not status_value:
            return error_response(
                "VALIDATION_ERROR", "status field is required.", status_code=400,
            )

        if status_value == 'cancelled':
            return self.cancel(request, pk=pk)
        if status_value == 'completed':
            return self.complete(request, pk=pk)

        return error_response(
            "INVALID_STATUS_TRANSITION",
            f"Cannot transition to '{status_value}' via this endpoint.",
            status_code=400,
        )

    # -- Reschedule (via booking engine) ------------------------------------

    @extend_schema(
        request=AppointmentRescheduleSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def reschedule(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dto = RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            new_start_at=serializer.validated_data['new_start_datetime'],
            initiator_role=(
                "specialist" if request.user.is_specialist else "client"
            ),
        )

        try:
            self.reschedule_booking_service_class().execute(dto)
        except SlotNotAvailableError as e:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(e), status_code=409,
            )
        except RescheduleNotAllowedError as e:
            return error_response(
                "RESCHEDULE_NOT_ALLOWED", str(e), status_code=422,
            )
        except InvalidStateTransitionError as e:
            return error_response(
                "INVALID_STATUS", str(e), status_code=422,
            )

        appointment.refresh_from_db()
        return success_response(AppointmentDetailSerializer(appointment).data)
