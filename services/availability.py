"""S3-CAL busy-source provider (#1044 / EPIC #317).

``ExternalBusyIntervalProvider`` implements the appointments
``BusyIntervalProvider`` Protocol so external (e.g. YClients) busy time composes
into the existing slot read path via ``make_read_provider()`` — with zero change
to ``SlotBuilderService`` / ``AvailabilityQueryService``.

Source-agnostic: it reads ``services.ExternalBusyInterval`` rows and knows
nothing about YClients (that coupling lives only in the webhook ingress).
"""
from __future__ import annotations

from datetime import datetime

from appointments.domain.value_objects import TimeInterval

from .models import ExternalBusyInterval


class ExternalBusyIntervalProvider:
    """Busy intervals from external calendars, clipped to the query window."""

    def get_busy_intervals(
        self,
        specialist_id,
        day_start_utc: datetime,
        day_end_utc: datetime,
    ) -> list[TimeInterval]:
        rows = ExternalBusyInterval.objects.filter(
            specialist_id=specialist_id,
            start_at__lt=day_end_utc,
            end_at__gt=day_start_utc,
        )
        intervals: list[TimeInterval] = []
        for row in rows:
            clipped_start = max(row.start_at, day_start_utc)
            clipped_end = min(row.end_at, day_end_utc)
            if clipped_start < clipped_end:
                intervals.append(
                    TimeInterval(start_at=clipped_start, end_at=clipped_end)
                )
        return intervals
