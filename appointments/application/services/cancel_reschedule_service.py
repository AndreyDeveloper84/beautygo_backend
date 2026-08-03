"""
CancelBookingService and RescheduleBookingService.

Both follow the same transaction pattern:
- Pre-transaction validation (cheap, no locks)
- Short atomic block (state transition + outbox)
- Post-commit side effects via outbox/workers
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from appointments.application.dto import (
    CancelBookingDTO,
    RescheduleBookingDTO,
    RescheduleResultDTO,
)
from appointments.application.services._booking_guards import apply_common_booking_guards
from appointments.domain.exceptions import (
    AppointmentTerminalError,
    ExpectedVersionRequiredError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from appointments.domain.policies import (
    BookingWindowPolicy,
    CancellationPolicy,
    DefaultBookingWindowPolicy,
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
from appointments.infrastructure.db_locks import specialist_advisory_lock

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
    """Reschedules a booking to a new time slot.

    Wave 1 Simple Reschedule hardening: the authoritative validation now
    happens INSIDE the locked atomic block, against the freshly-locked
    row — not against the pre-lock read taken in ``execute()``. The
    pre-lock check below is a cheap fail-fast only (avoids opening a
    transaction for an obviously-invalid request); it is never trusted
    on its own. See ``_execute_atomic`` for the authoritative order:
    terminal -> tenant -> version -> policy -> window/grid/time-off ->
    slot conflict.
    """

    def __init__(
        self,
        reschedule_policy: ReschedulePolicy | None = None,
        booking_window_policy: BookingWindowPolicy | None = None,
    ) -> None:
        self._policy = reschedule_policy or StandardReschedulePolicy()
        self._booking_window_policy = booking_window_policy or DefaultBookingWindowPolicy()

    def execute(self, dto: RescheduleBookingDTO) -> RescheduleResultDTO:
        from appointments.models import Appointment

        try:
            appointment = Appointment.objects.get(id=dto.booking_id)
        except Appointment.DoesNotExist:
            raise ValueError(f"Appointment {dto.booking_id} not found")

        current_status = BookingStatus(appointment.status)

        # Fail-fast only — see class docstring. Uses the pre-lock read
        # so an obviously-invalid request (wrong status, too-soon
        # notice) doesn't pay for opening a transaction + advisory lock.
        self._policy.can_reschedule(
            booking_status=current_status,
            booking_start_at=appointment.start_datetime,
            new_start_at=dto.new_start_at,
        )

        result = self._execute_atomic(
            booking_id=dto.booking_id,
            # specialist_id is immutable on an Appointment (invariant:
            # "service/specialist/price/currency/duration неизменны"),
            # so reading it here — outside the lock — is safe; it's used
            # only to pick the advisory-lock key before the row itself
            # is fetched under lock.
            specialist_id=appointment.specialist_id,
            new_start_at=dto.new_start_at,
            initiator_user_id=dto.initiator_user_id,
            initiator_role=dto.initiator_role,
            expected_version=dto.expected_version,
            tenant_id=dto.tenant_id,
            command_key=dto.command_key,
            basis=dto.basis,
        )

        logger.info(
            "booking.rescheduled booking_id=%s new_start=%s",
            dto.booking_id, dto.new_start_at.isoformat(),
        )
        return result

    @transaction.atomic
    def _execute_atomic(
        self,
        booking_id,
        specialist_id,
        new_start_at,
        initiator_user_id,
        initiator_role: str = "client",
        expected_version: int | None = None,
        tenant_id=None,
        command_key: str | None = None,
        basis: str = "mobile_app",
    ) -> RescheduleResultDTO:
        from appointments.models import Appointment, AppointmentRevision

        # Advisory lock FIRST — before the row lock — so a concurrent
        # create/reschedule for the same specialist serialises here
        # rather than racing an empty conflict-check queryset. See
        # infrastructure/db_locks.py.
        specialist_advisory_lock(specialist_id)

        appointment = Appointment.objects.select_for_update().get(id=booking_id)

        # One correlation_id for the whole command, shared by BOTH events
        # emitted below (targeted patch item 1) — lets a consumer/trace
        # tool see the canonical appointment.rescheduled and the legacy
        # booking.rescheduled as the same logical operation. Generated
        # once here (not per-emit) so a replayed idempotent command never
        # produces a second pair with a different id — no new emit call
        # happens on replay at all (the view layer's idempotency cache
        # returns the first response verbatim), so this is naturally
        # replay-stable without extra bookkeeping.
        command_correlation_id = uuid.uuid4()

        # --- Authoritative re-validation against the LOCKED row -----------
        # Order matters for error-message clarity: a booking that went
        # terminal concurrently should surface as "terminal", not as a
        # generic policy/version mismatch.
        if appointment.is_terminal:
            raise AppointmentTerminalError(
                f"Appointment {booking_id} is in terminal status "
                f"'{appointment.status}'"
            )

        if tenant_id is not None and appointment.tenant_id != tenant_id:
            raise TenantMismatchError(
                f"Appointment {booking_id} does not belong to tenant {tenant_id}"
            )

        if expected_version is not None:
            if appointment.version != expected_version:
                raise StaleVersionError(
                    f"Appointment {booking_id} expected_version={expected_version} "
                    f"but current version is {appointment.version}"
                )
        elif not settings.RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED:
            # Compatibility gate closed (owner/analytics decision) —
            # reject instead of silently allowing the lost-update race
            # below.
            raise ExpectedVersionRequiredError(
                f"Appointment {booking_id}: expected_version is required."
            )
        else:
            # Temporary mobile compatibility gate (code review
            # 2026-08-03, settings.RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED):
            # a caller that omits expected_version gets ZERO lost-update
            # protection here — two such requests can both pass this
            # check against the same version and silently overwrite each
            # other (the advisory lock above only serialises them, it
            # doesn't stop the second one from proceeding). This is a
            # KNOWN, tracked gap kept for backward compatibility with
            # mobile app builds that predate the version field, not an
            # oversight. Logged so it's observable/alertable pending the
            # owner decision to flip the gate closed.
            logger.warning(
                "reschedule.unversioned_command booking_id=%s basis=%s "
                "current_version=%s — expected_version omitted, "
                "lost-update protection skipped for this request.",
                booking_id, basis, appointment.version,
            )

        current_status = BookingStatus(appointment.status)
        self._policy.can_reschedule(
            booking_status=current_status,
            booking_start_at=appointment.start_datetime,
            new_start_at=new_start_at,
        )

        # Duration is immutable (invariant), so recomputing from the
        # LOCKED row's own start/end — not any pre-lock value — is both
        # correct and defensive.
        duration = appointment.end_datetime - appointment.start_datetime
        new_end_at = new_start_at + duration
        new_interval = TimeInterval(start_at=new_start_at, end_at=new_end_at)
        old_start_at = appointment.start_datetime
        old_end_at = appointment.end_datetime

        # Common create/reschedule guards: booking window (min-ahead +
        # horizon), slot-grid alignment, specialist time-off.
        apply_common_booking_guards(
            specialist_id, new_interval, self._booking_window_policy,
        )

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

        new_version = appointment.version + 1
        appointment.start_datetime = new_interval.start_at
        appointment.end_datetime = new_interval.end_at
        appointment.version = new_version
        appointment.save(update_fields=[
            "start_datetime", "end_datetime", "version", "updated_at",
        ])

        revision = AppointmentRevision.objects.create(
            appointment=appointment,
            version=new_version,
            old_start_datetime=old_start_at,
            old_end_datetime=old_end_at,
            new_start_datetime=new_interval.start_at,
            new_end_datetime=new_interval.end_at,
            actor_id=initiator_user_id,
            actor_role=initiator_role,
            basis=basis,
            command_key=command_key or "",
        )

        from appointments.infrastructure.outbox import (
            emit_outbox_event, safe_tenant_id,
        )
        from appointments.models import OutboxEvent as _OutboxEvent
        actor = (
            "admin" if initiator_role == "specialist"
            else "system" if initiator_role == "system"
            else "user"
        )
        rescheduled_by = (
            "master" if initiator_role == "specialist"
            else "system" if initiator_role == "system"
            else "user"
        )
        # Ayla Domain Event Registry v0.4 §6.3 (registered —
        # AYLA-DEC-0022 п.9) payload `actor` enum: user | specialist |
        # admin | owner | system | external_system. Distinct from the
        # ADR-0009 envelope `actor` bucket above (a coarser
        # "admin"/"system"/"user" classification shared by ALL outbox
        # events, used for internal routing) — the registry's canonical
        # payload wants the literal initiator role, so a
        # specialist-initiated reschedule must say "specialist" here,
        # not "admin". The envelope actor does NOT substitute for this
        # payload field (they answer different questions).
        registry_actor = (
            "specialist" if initiator_role == "specialist"
            else "system" if initiator_role == "system"
            else "user"
        )
        # Wave 1 only supports same-ID, time-only reschedule (no
        # specialist/service change — that's the "Replacement" path in
        # registry §6.3, out of scope here), so exactly one field ever
        # changes: the registry's `starts_at`. `ends_at` isn't a
        # registry-defined field at all (duration is invariant, so end
        # is fully derived from start + duration).
        changed_fields = ["starts_at"]
        tenant_id_for_event = safe_tenant_id(appointment, context="booking.rescheduled")

        # Canonical event (Wave 1 — see OutboxEvent.Topic.APPOINTMENT_RESCHEDULED
        # docstring). Payload shape is normatively defined by "Ayla
        # Domain Event Registry" v0.4 §6.3 (registered —
        # AYLA-DEC-0022 п.9): required
        # [appointment_id, version, previous_version, revision_id,
        # changed_fields, actor], optional [starts_at, previous_starts_at,
        # ...]. The envelope adds correlation_id/tenant_id automatically —
        # per registry §4 п.4, tenant_id belongs ONLY in the envelope,
        # not duplicated here.
        emit_outbox_event(
            topic=_OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
            data={
                "appointment_id": str(booking_id),
                "version": new_version,
                "previous_version": new_version - 1,
                "revision_id": str(revision.id),
                "changed_fields": changed_fields,
                "actor": registry_actor,
                "starts_at": new_interval.start_at.isoformat(),
                "previous_starts_at": old_start_at.isoformat(),
                # Non-canonical extras — the registry doesn't forbid
                # additional fields; kept for operational tracing. Not
                # read by any live consumer today: this topic is a
                # log-only stub (appointments/tasks.py) until the bot
                # migrates and OUTBOX_EXTERNAL_DELIVERY_TOPICS flips —
                # real cache invalidation/notifications stay wired to
                # the legacy booking.rescheduled event below.
                "specialist_id": str(appointment.specialist_id),
                "client_id": str(appointment.client_id),
                "old_end_at": old_end_at.isoformat(),
                "new_end_at": new_interval.end_at.isoformat(),
                "basis": basis,
                "command_key": command_key,
            },
            user_id=appointment.client_id,
            tenant_id=tenant_id_for_event,
            actor=actor,
            correlation_id=command_correlation_id,
        )

        # Legacy alias — UNCHANGED shape from pre-Wave-1. Kept so the
        # not-yet-migrated bot consumer keeps working; do not remove
        # until the bot-side wave (04_AGENT_BOT_IMPLEMENTATION.md)
        # migrates to the canonical topic above. Both events are written
        # in this same transaction.
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
                "rescheduled_by": rescheduled_by,
            },
            user_id=appointment.client_id,
            tenant_id=tenant_id_for_event,
            # Same actor mapping as the cancel emit above — see that
            # comment for the rationale.
            actor=actor,
            correlation_id=command_correlation_id,
        )

        return RescheduleResultDTO(
            booking_id=booking_id,
            version=new_version,
            revision_id=revision.id,
            correlation_id=command_correlation_id,
        )
