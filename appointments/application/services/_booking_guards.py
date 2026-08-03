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


def apply_common_booking_guards(
    specialist_id: UUID,
    target_interval: TimeInterval,
    booking_window_policy: BookingWindowPolicy | None = None,
) -> None:
    """Run the shared create/reschedule guards against ``target_interval``.

    Raises ``BookingWindowError`` (min-ahead / horizon / off-grid) or
    ``SlotNotAvailableError`` (time-off block). Callers run this inside
    the locked atomic block so the time-off check sees committed state.
    """
    (booking_window_policy or DefaultBookingWindowPolicy()).validate_booking_window(
        target_interval.start_at
    )
    _validate_grid_alignment(target_interval.start_at)
    _check_time_off(specialist_id, target_interval)
