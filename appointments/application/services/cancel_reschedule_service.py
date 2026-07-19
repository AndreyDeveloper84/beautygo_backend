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
from django.utils import timezone

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

# Trusted internal reason tokens → §3.2 reason_code. ONLY server-side
# callers populate ``reason`` with these tokens (e.g. the specialist-
# departure cascade in users/services.py). API free-text reason (an
# unvalidated CharField, AppointmentCancelSerializer.reason) MUST NOT be
# able to drive the attribution enum — a client cancelling their own
# booking could otherwise send reason="user_no_show"/"master_unavailable"
# and forge a reason_code that contradicts cancelled_by. So there is no
# literal §3.2 passthrough: an unrecognised reason falls back to the
# initiator-role default below.
_REASON_TOKEN_TO_CODE = {
    "specialist_departure": "master_unavailable",
}
# initiator_role → (cancelled_by enum, fallback reason_code). System falls
# back to "other" (never None); §3.2 has no generic system-auto code.
_ROLE_TO_CANCELLED_BY = {
    "specialist": ("master", "master_unavailable"),
    "system": ("system", "other"),
}  # default → ("user", "user_changed_plans")


def _resolve_cancellation_vocab(initiator_role: str, reason: str | None):
    """Map (initiator_role, reason) → (cancelled_by, reason_code).

    ``reason_code`` is derived ONLY from a trusted internal reason token
    (``_REASON_TOKEN_TO_CODE``, populated by server-side callers) or the
    initiator-role default — never from raw API free-text, which would
    let a client forge the attribution enum. The free-text ``reason``
    itself travels separately in the human-readable payload field. The
    result is always a non-null §3.2 ``reason_code``.
    """
    cancelled_by, fallback = _ROLE_TO_CANCELLED_BY.get(
        initiator_role, ("user", "user_changed_plans"))
    code = None
    if reason:
        code = _REASON_TOKEN_TO_CODE.get(reason.strip().lower())
    return cancelled_by, (code or fallback)


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

        # Acceptance #5: booking cancelled ⇒ the hold is released
        # automatically (D6/D8). Runs AFTER the commit — a provider
        # outage never blocks the cancellation itself; failures are
        # logged and left for the reconciliation job. No-op when the
        # booking has no authorized hold (no-prepayment path).
        from payments.services import cancel_authorized_hold_for_appointment
        try:
            cancel_authorized_hold_for_appointment(appointment)
        except Exception:  # noqa: BLE001 — same post-commit rationale
            logger.exception(
                'hold.cancel_hook_failed booking_id=%s', dto.booking_id,
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
        from appointments.models import Appointment

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

        from appointments.infrastructure.outbox import (
            emit_outbox_event, safe_tenant_id,
        )
        from appointments.models import OutboxEvent as _OutboxEvent
        # Contract §3.2 vocabulary (consumers/booking.py reads
        # data["cancelled_by"] + data["reason_code"]). Map the internal
        # initiator_role {client, specialist, system} → the closed
        # cancelled_by enum {user, master, system} and resolve a non-null
        # §3.2 reason_code from a trusted internal token or the role
        # default (NOT from API free-text). The free-text ``reason`` stays
        # in the payload for human/audit context only.
        cancelled_by, reason_code = _resolve_cancellation_vocab(
            initiator_role, reason,
        )
        emit_outbox_event(
            topic=_OutboxEvent.Topic.BOOKING_CANCELLED,
            data={
                "appointment_id": str(booking_id),
                "specialist_id": str(appointment.specialist_id),
                "start_at": appointment.start_datetime.isoformat(),
                "cancelled_by": cancelled_by,
                "reason_code": reason_code,
                "cancelled_at": timezone.now().isoformat(),
                "initiator_role": initiator_role,
                "refund_percent": refund_percent,
                "reason": reason,
            },
            user_id=initiator_user_id,
            tenant_id=safe_tenant_id(appointment, context="booking.cancelled"),
            # initiator_role contract (see dto.py:36) is the closed set
            # {client, specialist, system}. Map to ADR-0009 actor:
            #   client    → 'user'   (client cancels via mobile)
            #   specialist→ 'admin'  (provider-side action; closer to
            #                         'admin' than 'user' per ADR-0009
            #                         §Mandatory event contract)
            #   system    → 'system' (rare — TTL sweep, batch job, etc.)
            actor=(
                "admin" if initiator_role == "specialist"
                else "system" if initiator_role == "system"
                else "user"
            ),
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
            initiator_role=dto.initiator_role,
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
        initiator_role: str = "client",
    ) -> None:
        from appointments.models import Appointment

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

        from appointments.infrastructure.outbox import (
            emit_outbox_event, safe_tenant_id,
        )
        from appointments.models import OutboxEvent as _OutboxEvent
        emit_outbox_event(
            topic=_OutboxEvent.Topic.BOOKING_RESCHEDULED,
            data={
                "appointment_id": str(booking_id),
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
                # Contract §3.3 field the bot consumer hard-reads
                # (consumers/booking.py: data["new_start_at"]). Same
                # value as start_at above; both kept so the in-process
                # cache handler (reads start_at) and the cross-service
                # consumer (reads new_start_at) each find their key.
                "new_start_at": new_interval.start_at.isoformat(),
                "old_start_at": old_start_at.isoformat(),
                "rescheduled_by": (
                    "master" if initiator_role == "specialist"
                    else "system" if initiator_role == "system"
                    else "user"
                ),
            },
            user_id=appointment.client_id,
            tenant_id=safe_tenant_id(appointment, context="booking.rescheduled"),
            # Same actor mapping as the cancel emit above — see that
            # comment for the rationale.
            actor=(
                "admin" if initiator_role == "specialist"
                else "system" if initiator_role == "system"
                else "user"
            ),
        )
