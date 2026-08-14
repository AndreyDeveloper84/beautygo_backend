"""Shared booking guards — Wave 1 Simple Reschedule hardening.

Reusable checks against a target interval: booking-window (min-ahead +
horizon), slot-grid alignment, and specialist time-off.
``RescheduleBookingService`` wires all three (it previously only checked
a 4h-notice rule, not window/grid/time-off at all). ``CreateBookingService``
already has its own window + time-off checks (pre-dating this module,
now interleaved with AMD-019 salon/marketplace service resolution) and
is intentionally left untouched here — reusing this module there is a
reasonable future refactor, but grid-alignment is a genuinely new
constraint and forcing it onto create's several call paths (walk-in,
salon, external-busy) without auditing each one first risks a regression
outside this task's scope.

DRF-1062 adds two more checks — ``check_schedule_frame`` and
``check_tenant_closure`` — and these ARE called from create as well, as
individual functions rather than through ``apply_common_booking_guards``.
That keeps the split above intact (create still does not inherit
grid-alignment) while guaranteeing the thing this task exists to
guarantee: the schedule is enforced identically on every write path.

Grid-alignment note: the check is UTC-minute-modulo, not per-specialist-
timezone-aware. This is exact for the pilot's whole-hour-offset timezones
(Russia/Kazakhstan) and matches the precedent already set by
``infrastructure/availability/slot_builder.py``, which steps its display
grid in UTC too. A half-hour-offset timezone would need a timezone-aware
version of this check — out of scope until the pilot expands there.
"""
from __future__ import annotations

from uuid import UUID

from django.conf import settings

from appointments.domain.exceptions import BookingWindowError, SlotNotAvailableError
from appointments.domain.policies import BookingWindowPolicy, DefaultBookingWindowPolicy
from appointments.domain.value_objects import TimeInterval


def _validate_grid_alignment(start_at) -> None:
    grid_minutes = int(getattr(settings, 'BOOKING_SLOT_GRID_MINUTES', 30))
    if (
        start_at.second != 0
        or start_at.microsecond != 0
        or start_at.minute % grid_minutes != 0
    ):
        raise BookingWindowError(
            f"Start time must align to the {grid_minutes}-minute slot grid"
        )


def _check_time_off(specialist_id: UUID, target_interval: TimeInterval) -> None:
    from appointments.models import SpecialistTimeOff

    blocked = SpecialistTimeOff.objects.filter(
        specialist_id=specialist_id,
        start_at__lt=target_interval.end_at,
        end_at__gt=target_interval.start_at,
    ).exists()
    if blocked:
        raise SlotNotAvailableError(
            f"Slot {target_interval} is blocked by specialist"
        )


def check_schedule_frame(specialist_id: UUID, target_interval: TimeInterval) -> None:
    """Reject bookings that fall outside the specialist's working day.

    DRF-1062. Until this existed, the weekly schedule was enforced on the
    READ path only: ``SpecialistWorkingHours`` was read by
    ``AvailabilityQueryService`` and by nothing else, so a direct POST for
    a Sunday 10:00 was accepted even with Sunday marked non-working. Slots
    stopped being *offered*, but the salon stayed *bookable* — a stale
    client, a replayed request or a retry was enough.

    The frame is resolved through ``AvailabilityQueryService`` rather than
    re-queried here on purpose: read and write must never again disagree
    about what "working hours" means. That resolver honours the per-date
    ``SpecialistScheduleException`` override as well as the weekly
    template.

    Salon-wide closures are enforced separately, as busy intervals, since
    they only subtract (see ``TenantClosureBusyIntervalProvider``).

    Scope note: the interval checked is the service duration, without
    ``buffer_after_minutes``. The read path additionally requires the
    buffer to fit before closing time, so a booking whose trailing buffer
    overruns closing is offered by neither path but accepted here. That
    asymmetry is deliberate — coupling this guard to service resolution
    would drag catalog lookups into every write path for a case that
    costs nothing operationally.
    """
    from zoneinfo import ZoneInfo

    from appointments.application.services.availability_query_service import (
        AvailabilityQueryService,
    )
    from appointments.infrastructure.availability.slot_builder import (
        SlotBuilderService,
    )
    from users.models import SpecialistProfile

    specialist = SpecialistProfile.objects.filter(id=specialist_id).first()
    if specialist is None:
        # Existence is the caller's contract; this guard only rules on
        # timing and must not turn a missing specialist into a 409.
        return

    tz = ZoneInfo(specialist.timezone)
    local_date = target_interval.start_at.astimezone(tz).date()

    frame = AvailabilityQueryService._get_working_hours(specialist, local_date)
    if frame is None:
        raise SlotNotAvailableError(
            f"Slot {target_interval} falls on a non-working day"
        )

    to_utc = SlotBuilderService._local_time_to_utc
    work_start_utc = to_utc(local_date, frame["start"], tz)
    work_end_utc = to_utc(local_date, frame["end"], tz)

    if (
        target_interval.start_at < work_start_utc
        or target_interval.end_at > work_end_utc
    ):
        raise SlotNotAvailableError(
            f"Slot {target_interval} falls outside working hours"
        )

    if frame["break_start"] and frame["break_end"]:
        break_interval = TimeInterval(
            start_at=to_utc(local_date, frame["break_start"], tz),
            end_at=to_utc(local_date, frame["break_end"], tz),
        )
        if target_interval.overlaps(break_interval):
            raise SlotNotAvailableError(
                f"Slot {target_interval} overlaps the specialist's break"
            )


def check_tenant_closure(specialist_id: UUID, target_interval: TimeInterval) -> None:
    """Reject bookings that fall inside a salon-wide closure (DRF-1062).

    Reuses the same provider the read path composes, so a closure can
    never be visible to slot generation and invisible to booking.
    """
    from appointments.infrastructure.availability.providers import (
        TenantClosureBusyIntervalProvider,
    )

    closed = TenantClosureBusyIntervalProvider().get_busy_intervals(
        specialist_id=specialist_id,
        day_start_utc=target_interval.start_at,
        day_end_utc=target_interval.end_at,
    )
    if closed:
        raise SlotNotAvailableError(
            f"Slot {target_interval} falls inside a salon closure"
        )


def apply_common_booking_guards(
    specialist_id: UUID,
    target_interval: TimeInterval,
    booking_window_policy: BookingWindowPolicy | None = None,
) -> None:
    """Run the shared create/reschedule guards against ``target_interval``.

    Raises ``BookingWindowError`` (min-ahead / horizon / off-grid) or
    ``SlotNotAvailableError`` (outside working hours, break, salon closure
    or time-off block). Callers run this inside the locked atomic block so
    every check sees committed state.
    """
    (booking_window_policy or DefaultBookingWindowPolicy()).validate_booking_window(
        target_interval.start_at
    )
    _validate_grid_alignment(target_interval.start_at)
    check_schedule_frame(specialist_id, target_interval)
    check_tenant_closure(specialist_id, target_interval)
    _check_time_off(specialist_id, target_interval)
