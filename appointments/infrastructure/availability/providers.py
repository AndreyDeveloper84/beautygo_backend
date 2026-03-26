"""
Availability providers for Booking Engine.

Each source of "busy time" is a separate provider implementing BusyIntervalProvider.
New sources (external calendars, equipment, rooms) are added as new classes,
without touching AvailabilityQueryService.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from appointments.domain.value_objects import TimeInterval, ACTIVE_BOOKING_STATUSES


@runtime_checkable
class BusyIntervalProvider(Protocol):
    """Contract for any source of busy intervals."""

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        ...


class BookingBusyIntervalProvider:
    """
    Returns busy intervals from active (non-cancelled) appointments.

    Uses select_for_update when called from write path (conflict check).
    """

    def __init__(self, for_update: bool = False) -> None:
        self._for_update = for_update

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        from appointments.models import Appointment

        qs = Appointment.objects.filter(
            specialist_id=specialist_id,
            start_datetime__lt=day_end_utc,
            end_datetime__gt=day_start_utc,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
        )

        if self._for_update:
            qs = qs.select_for_update(nowait=False)

        return [
            TimeInterval(start_at=a.start_datetime, end_at=a.end_datetime)
            for a in qs
        ]


class TimeOffBusyIntervalProvider:
    """Returns busy intervals from specialist time-off blocks."""

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        from appointments.models import SpecialistTimeOff

        blocks = SpecialistTimeOff.objects.filter(
            specialist_id=specialist_id,
            start_at__lt=day_end_utc,
            end_at__gt=day_start_utc,
        )

        intervals = []
        for block in blocks:
            clipped_start = max(block.start_at, day_start_utc)
            clipped_end = min(block.end_at, day_end_utc)
            if clipped_start < clipped_end:
                intervals.append(TimeInterval(
                    start_at=clipped_start,
                    end_at=clipped_end,
                ))
        return intervals


class CompositeAvailabilityProvider:
    """Aggregates multiple BusyIntervalProvider implementations."""

    def __init__(self, providers: list[BusyIntervalProvider]) -> None:
        self._providers = providers

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        all_intervals: list[TimeInterval] = []
        for provider in self._providers:
            all_intervals.extend(
                provider.get_busy_intervals(specialist_id, day_start_utc, day_end_utc)
            )
        return all_intervals


def make_read_provider() -> CompositeAvailabilityProvider:
    """Factory for read-path availability (no locking)."""
    return CompositeAvailabilityProvider([
        BookingBusyIntervalProvider(for_update=False),
        TimeOffBusyIntervalProvider(),
    ])


def make_write_provider() -> CompositeAvailabilityProvider:
    """Factory for write-path conflict check (with row locking)."""
    return CompositeAvailabilityProvider([
        BookingBusyIntervalProvider(for_update=True),
        TimeOffBusyIntervalProvider(),
    ])
