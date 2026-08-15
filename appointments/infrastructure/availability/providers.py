"""
Availability providers for Booking Engine.

Each source of "busy time" is a separate provider implementing BusyIntervalProvider.
New sources (external calendars, equipment, rooms) are added as new classes,
without touching AvailabilityQueryService.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
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


class TenantClosureBusyIntervalProvider:
    """Returns busy intervals from salon-wide closures (DRF-1062).

    A closure is stored once per tenant as a local date (plus optional
    local wall-clock window) and is resolved to UTC **here**, using the
    specialist's own timezone — a ``Tenant`` has none of its own. That
    keeps one stored decision correct for specialists in different
    timezones, which matters once the pilot expands past Moscow.
    """

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        from zoneinfo import ZoneInfo

        from appointments.models import TenantClosure
        from users.models import SpecialistProfile

        row = (
            SpecialistProfile.objects
            .filter(id=specialist_id)
            .values('tenant_id', 'timezone')
            .first()
        )
        # tenant is nullable on SpecialistProfile — a specialist outside
        # any salon simply has no closures to honour.
        if not row or not row['tenant_id']:
            return []

        tz = ZoneInfo(row['timezone'])

        # Widen by a day on each side: a UTC window can straddle three
        # local dates once offsets are applied, and over-fetching a row
        # is cheaper than missing a closure. Clipping below discards
        # anything that does not actually overlap.
        first_local_date = (day_start_utc.astimezone(tz) - timedelta(days=1)).date()
        last_local_date = (day_end_utc.astimezone(tz) + timedelta(days=1)).date()

        closures = TenantClosure.objects.filter(
            tenant_id=row['tenant_id'],
            date__gte=first_local_date,
            date__lte=last_local_date,
        )

        intervals: list[TimeInterval] = []
        for closure in closures:
            if closure.start_time is None:
                # Whole day: local midnight to local midnight. Built from
                # two dates rather than +24h so DST transitions keep the
                # closure aligned to the calendar day.
                local_start = datetime.combine(
                    closure.date, time(0, 0), tzinfo=tz,
                )
                local_end = datetime.combine(
                    closure.date + timedelta(days=1), time(0, 0), tzinfo=tz,
                )
            else:
                local_start = datetime.combine(
                    closure.date, closure.start_time, tzinfo=tz,
                )
                local_end = datetime.combine(
                    closure.date, closure.end_time, tzinfo=tz,
                )

            clipped_start = max(local_start.astimezone(timezone.utc), day_start_utc)
            clipped_end = min(local_end.astimezone(timezone.utc), day_end_utc)
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


def _external_busy_providers() -> list[BusyIntervalProvider]:
    """S3-CAL: external busy source, composed only when the flag is on.

    Off (default) → inert, booking behaviour unchanged. Import is lazy to
    avoid an app-load import cycle (services imports appointments value
    objects).
    """
    from django.conf import settings

    if not getattr(settings, "EXTERNAL_BUSY_ENABLED", False):
        return []
    from services.availability import ExternalBusyIntervalProvider
    return [ExternalBusyIntervalProvider()]


def make_read_provider() -> CompositeAvailabilityProvider:
    """Factory for read-path availability (no locking)."""
    return CompositeAvailabilityProvider([
        BookingBusyIntervalProvider(for_update=False),
        TimeOffBusyIntervalProvider(),
        TenantClosureBusyIntervalProvider(),
        *_external_busy_providers(),
    ])


def make_write_provider() -> CompositeAvailabilityProvider:
    """Factory for write-path conflict check (with row locking)."""
    return CompositeAvailabilityProvider([
        BookingBusyIntervalProvider(for_update=True),
        TimeOffBusyIntervalProvider(),
        TenantClosureBusyIntervalProvider(),
        *_external_busy_providers(),
    ])
