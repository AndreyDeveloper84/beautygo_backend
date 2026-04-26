"""
CancelBookingService and RescheduleBookingService.

Both follow the same transaction pattern:
- Pre-transaction validation (cheap, no locks)
- Short atomic block (state transition + outbox)
- Post-commit side effects via outbox/workers
"""

from __future__ import annotations

import logging

from django.db import transaction

from appointments.application.dto import CancelBookingDTO, RescheduleBookingDTO
from appointments.domain.exceptions import SlotNotAvailableError
from appointments.domain.policies import (
    CancellationPolicy,
    ReschedulePolicy,
    StandardCancellationPolicy,
    StandardReschedulePolicy,
)
from appointments.domain.value_objects import (
    BookingStatus,
    BookingStateMachine,
    TimeInterval,
    ACTIVE_BOOKING_STATUSES,
)

logger = logging.getLogger(__name__)


class CancelBookingService:
    """Cancels a booking according to cancellation policy."""

    def __init__(
        self,
        cancellation_policy: CancellationPolicy | None = None,
    ) -> None:
        self._policy = cancellation_policy or StandardCancellationPolicy()

    def execute(self, dto: CancelBookingDTO) -> None:
        from appointments.models import Appointment

        try:
            appointment = Appointment.objects.get(id=dto.booking_id)
        except Appointment.DoesNotExist:
            raise ValueError(f"Appointment {dto.booking_id} not found")

        current_status = BookingStatus(appointment.status)

        self._policy.can_cancel(
            booking_status=current_status,
            booking_start_at=appointment.start_datetime,
            initiator=dto.initiator_role,
        )

        refund_percent = self._policy.get_refund_percent(
            booking_start_at=appointment.start_datetime,
            initiator=dto.initiator_role,
        )

        self._execute_atomic(
            booking_id=dto.booking_id,
            current_status=current_status,
            initiator_user_id=dto.initiator_user_id,
            initiator_role=dto.initiator_role,
            refund_percent=refund_percent,
            reason=dto.reason,
        )

        logger.info(
            "booking.cancelled booking_id=%s initiator=%s refund_percent=%s",
            dto.booking_id, dto.initiator_role, refund_percent,
        )

    @transaction.atomic
    def _execute_atomic(
        self,
        booking_id,
        current_status: BookingStatus,
        initiator_user_id,
        initiator_role: str,
        refund_percent: float,
        reason: str | None,
    ) -> None:
        from appointments.models import Appointment, OutboxEvent

        appointment = Appointment.objects.select_for_update().get(id=booking_id)

        current_status = BookingStatus(appointment.status)
        new_status = BookingStateMachine.transition(
            current_status, BookingStatus.CANCELLED,
        )

        appointment.status = new_status.value
        appointment.cancellation_reason = reason or ""
        appointment.cancelled_by_id = initiator_user_id
        appointment.save(update_fields=[
            "status", "cancellation_reason", "cancelled_by_id", "updated_at",
        ])

        OutboxEvent.objects.create(
            topic="booking.cancelled",
            payload={
                "booking_id": str(booking_id),
                "specialist_id": str(appointment.specialist_id),
                "start_at": appointment.start_datetime.isoformat(),
                "initiator_role": initiator_role,
                "refund_percent": refund_percent,
                "reason": reason,
            },
        )


class RescheduleBookingService:
    """Reschedules a booking to a new time slot."""

    def __init__(
        self,
        reschedule_policy: ReschedulePolicy | None = None,
    ) -> None:
        self._policy = reschedule_policy or StandardReschedulePolicy()

    def execute(self, dto: RescheduleBookingDTO) -> None:
        from appointments.models import Appointment

        try:
            appointment = Appointment.objects.get(id=dto.booking_id)
        except Appointment.DoesNotExist:
            raise ValueError(f"Appointment {dto.booking_id} not found")

        current_status = BookingStatus(appointment.status)

        self._policy.can_reschedule(
            booking_status=current_status,
            booking_start_at=appointment.start_datetime,
            new_start_at=dto.new_start_at,
        )

        duration = appointment.end_datetime - appointment.start_datetime
        new_end_at = dto.new_start_at + duration
        new_interval = TimeInterval(start_at=dto.new_start_at, end_at=new_end_at)

        self._execute_atomic(
            booking_id=dto.booking_id,
            old_start_at=appointment.start_datetime,
            new_interval=new_interval,
        )

        logger.info(
            "booking.rescheduled booking_id=%s new_start=%s",
            dto.booking_id, dto.new_start_at.isoformat(),
        )

    @transaction.atomic
    def _execute_atomic(
        self,
        booking_id,
        old_start_at,
        new_interval: TimeInterval,
    ) -> None:
        from appointments.models import Appointment, OutboxEvent

        appointment = Appointment.objects.select_for_update().get(id=booking_id)

        conflicting = Appointment.objects.filter(
            specialist_id=appointment.specialist_id,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=new_interval.end_at,
            end_datetime__gt=new_interval.start_at,
        ).exclude(id=booking_id).select_for_update().exists()

        if conflicting:
            raise SlotNotAvailableError(
                f"New slot {new_interval} is already taken"
            )

        appointment.start_datetime = new_interval.start_at
        appointment.end_datetime = new_interval.end_at
        appointment.save(update_fields=[
            "start_datetime", "end_datetime", "updated_at",
        ])

        OutboxEvent.objects.create(
            topic="booking.rescheduled",
            payload={
                "booking_id": str(booking_id),
                "specialist_id": str(appointment.specialist_id),
                "client_id": str(appointment.client_id),
                # Use the same start_at/end_at field names as booking.created
                # / booking.cancelled events so handle_booking_rescheduled →
                # _invalidate_slots_from_payload finds the *new* date for
                # cache invalidation. old_start_at handled separately by
                # the same handler. Without this, new-date slots stayed
                # stale after a reschedule (#12 in REFACTOR_PRIORITIZATION).
                "start_at": new_interval.start_at.isoformat(),
                "end_at": new_interval.end_at.isoformat(),
                "old_start_at": old_start_at.isoformat(),
            },
        )
