"""Appointment views — thin views delegating to application services."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import QuerySet
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
    BookingDomainError,
    BookingWindowError,
    CancellationNotAllowedError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    ServiceNotActiveError,
    SlotNotAvailableError,
    SpecialistNotActiveError,
)
from .models import Appointment
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

    def get_queryset(self) -> QuerySet:
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

    # -- List ----------------------------------------------------------------

    def list(self, request: Request) -> Response:
        qs = self.get_queryset()
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        serializer = AppointmentListSerializer(qs, many=True)
        return success_response(serializer.data)

    # -- Create (via booking engine) ----------------------------------------

    def create(self, request: Request) -> Response:
        if not request.user.is_client:
            return error_response(
                "FORBIDDEN", "Only clients can create appointments.",
                status_code=403,
            )

        serializer = AppointmentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

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

        try:
            service = CreateBookingService()
            result = service.execute(dto)
        except (SpecialistNotActiveError, ServiceNotActiveError) as e:
            return error_response(
                "BUSINESS_ERROR", str(e), status_code=400,
            )
        except BookingWindowError as e:
            return error_response(
                "BOOKING_WINDOW_ERROR", str(e), status_code=400,
            )
        except SlotNotAvailableError as e:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(e), status_code=409,
            )
        except BookingDomainError as e:
            return error_response(
                "BOOKING_ERROR", str(e), status_code=400,
            )

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

    def retrieve(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Cancel (via booking engine) ----------------------------------------

    @action(detail=True, methods=['post'])
    def cancel(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentCancelSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        dto = CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            initiator_role="specialist" if request.user.is_specialist else "client",
            reason=serializer.validated_data.get('reason', ''),
        )

        try:
            CancelBookingService().execute(dto)
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

    @action(detail=True, methods=['post'])
    def complete(self, request: Request, pk: Any = None) -> Response:
        if not request.user.is_specialist:
            return error_response(
                "FORBIDDEN",
                "Only specialists can mark appointments as complete.",
                status_code=403,
            )
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )
        try:
            appointment.complete()
        except DjangoValidationError as e:
            return error_response(
                "INVALID_STATUS", str(e.message), status_code=422,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Reschedule (via booking engine) ------------------------------------

    @action(detail=True, methods=['post'])
    def reschedule(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentRescheduleSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )

        dto = RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            new_start_at=serializer.validated_data['new_start_datetime'],
        )

        try:
            RescheduleBookingService().execute(dto)
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
