"""
CreateBookingService — the critical write path of the Booking Engine.

Steps:
1-4: Pre-transaction validation (cheap; no locks)
5-12: Atomic write path (inside transaction)
13: Post-commit side effects (via outbox worker)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from appointments.application.dto import CreateBookingDTO, BookingResultDTO
from appointments.domain.exceptions import (
    SlotNotAvailableError,
    SpecialistNotActiveError,
    ServiceNotActiveError,
)
from appointments.domain.policies import (
    CommissionPolicy,
    BookingWindowPolicy,
    DefaultCommissionPolicy,
    DefaultBookingWindowPolicy,
)
from appointments.domain.value_objects import (
    BookingStatus,
    BookingSnapshot,
    TimeInterval,
    ACTIVE_BOOKING_STATUSES,
)

logger = logging.getLogger(__name__)


class CreateBookingService:
    """Orchestrates the booking creation use case."""

    def __init__(
        self,
        commission_policy: CommissionPolicy | None = None,
        booking_window_policy: BookingWindowPolicy | None = None,
    ) -> None:
        self._commission_policy = commission_policy or DefaultCommissionPolicy()
        self._booking_window_policy = booking_window_policy or DefaultBookingWindowPolicy()

    def execute(self, dto: CreateBookingDTO) -> BookingResultDTO:
        specialist, service = self._validate_pre_transaction(dto)

        end_at = dto.start_at + timedelta(minutes=service.duration_minutes)
        target_interval = TimeInterval(start_at=dto.start_at, end_at=end_at)

        appointment, payment = self._execute_atomic(
            dto=dto,
            specialist=specialist,
            service=service,
            target_interval=target_interval,
        )

        logger.info(
            "booking.created booking_id=%s specialist=%s start=%s",
            appointment.id, dto.specialist_id, dto.start_at.isoformat(),
        )

        return BookingResultDTO(
            booking_id=appointment.id,
            status=appointment.status,
            start_at=appointment.start_datetime,
            end_at=appointment.end_datetime,
            service_name=service.name,
            duration_minutes=service.duration_minutes,
            price=str(service.price),
            payment_id=payment.id if payment else None,
            payment_client_secret=None,
        )

    def _validate_pre_transaction(self, dto: CreateBookingDTO):
        from users.models import SpecialistProfile
        from services.models import Service

        try:
            specialist = SpecialistProfile.objects.select_related("user").get(
                id=dto.specialist_id
            )
        except SpecialistProfile.DoesNotExist:
            raise SpecialistNotActiveError(
                f"Specialist {dto.specialist_id} not found"
            )

        try:
            service = Service.objects.get(
                id=dto.service_id, specialist=specialist,
            )
        except Service.DoesNotExist:
            raise ServiceNotActiveError(
                f"Service {dto.service_id} not found"
            )

        if not specialist.is_booking_enabled:
            raise SpecialistNotActiveError("Specialist is not accepting bookings")

        if specialist.status != SpecialistProfile.ProfileStatus.ACTIVE:
            raise SpecialistNotActiveError("Specialist profile is not active")

        if not service.is_active:
            raise ServiceNotActiveError("This service is not available for booking")

        self._booking_window_policy.validate_booking_window(dto.start_at)

        if dto.start_at.tzinfo is None:
            raise ValueError("start_at must be timezone-aware (UTC)")

        return specialist, service

    @transaction.atomic
    def _execute_atomic(self, dto, specialist, service, target_interval: TimeInterval):
        from appointments.models import Appointment, Payment, OutboxEvent, SpecialistTimeOff

        # Idempotency check
        existing = Appointment.objects.filter(
            idempotency_key=dto.idempotency_key
        ).select_for_update().first()

        if existing:
            logger.info(
                "booking.idempotent_return booking_id=%s key=%s",
                existing.id, dto.idempotency_key,
            )
            return existing, existing.payments.filter(status="pending").first()

        # Conflict check with row-level lock
        conflicting_count = Appointment.objects.filter(
            specialist_id=dto.specialist_id,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=target_interval.end_at,
            end_datetime__gt=target_interval.start_at,
        ).select_for_update().count()

        if conflicting_count > 0:
            raise SlotNotAvailableError(
                f"Slot {target_interval} is already taken"
            )

        # Check time-off blocks
        blocked = SpecialistTimeOff.objects.filter(
            specialist_id=dto.specialist_id,
            start_at__lt=target_interval.end_at,
            end_at__gt=target_interval.start_at,
        ).exists()

        if blocked:
            raise SlotNotAvailableError(
                f"Slot {target_interval} is blocked by specialist"
            )

        # First vs repeat visit
        is_first_visit = not Appointment.objects.filter(
            specialist_id=dto.specialist_id,
            client_id=dto.client_id,
            status=BookingStatus.COMPLETED.value,
        ).exists()

        # Commission snapshot
        commission_percent = Decimal(str(
            self._commission_policy.get_percent(dto.client_id, dto.specialist_id)
        ))
        snapshot = BookingSnapshot.create(
            service_name=service.name,
            duration_minutes=service.duration_minutes,
            price=service.price,
            commission_percent=commission_percent,
            specialist_timezone=specialist.timezone,
            buffer_after_minutes=getattr(service, "buffer_after_minutes", 0),
        )

        # Create appointment
        appointment = Appointment.objects.create(
            client_id=dto.client_id,
            specialist_id=dto.specialist_id,
            service_id=dto.service_id,
            start_datetime=target_interval.start_at,
            end_datetime=target_interval.end_at,
            status=BookingStatus.AWAITING_PAYMENT.value,
            idempotency_key=dto.idempotency_key,
            is_first_visit=is_first_visit,
            price=snapshot.price,
            snapshot_service_name=snapshot.service_name,
            snapshot_duration_minutes=snapshot.service_duration_minutes,
            snapshot_price=snapshot.price,
            snapshot_commission_percent=snapshot.commission_percent,
            snapshot_specialist_income=snapshot.specialist_income,
            snapshot_platform_fee=snapshot.platform_fee,
            snapshot_timezone=snapshot.specialist_timezone,
        )

        # Create payment record
        payment = Payment.objects.create(
            appointment=appointment,
            amount=snapshot.price,
            status="pending",
            specialist_income=snapshot.specialist_income,
            platform_fee=snapshot.platform_fee,
        )

        # Write outbox event (same transaction)
        OutboxEvent.objects.create(
            topic="booking.created",
            payload={
                "booking_id": str(appointment.id),
                "client_id": str(dto.client_id),
                "specialist_id": str(dto.specialist_id),
                "service_id": str(dto.service_id),
                "start_at": target_interval.start_at.isoformat(),
                "end_at": target_interval.end_at.isoformat(),
                "payment_id": str(payment.id),
                "amount": str(snapshot.price),
                "specialist_timezone": snapshot.specialist_timezone,
            },
        )

        return appointment, payment
