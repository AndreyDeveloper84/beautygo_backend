"""
SlotBuilderService — generates available time slots for a specialist on a given date.

All internal computation in UTC; local time only for output display.
Handles DST transitions correctly via ZoneInfo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from appointments.domain.value_objects import TimeInterval
from appointments.application.dto import AvailableSlotDTO, DayAvailabilityDTO

from django.conf import settings

# Grid interval — every 30 minutes a new slot can start
SLOT_GRID_MINUTES = int(getattr(settings, 'BOOKING_SLOT_GRID_MINUTES', 30))

# Minimum lead time before a slot is shown to clients
MIN_BOOKING_AHEAD_MINUTES = int(getattr(settings, 'BOOKING_MIN_AHEAD_MINUTES', 60))


class SlotBuilderService:
    """Builds a list of available slots for a specialist on a given date."""

    def build(
        self,
        target_date: date,
        specialist_timezone: str,
        working_start_local: str,
        working_end_local: str,
        service_duration_minutes: int,
        buffer_after_minutes: int,
        busy_intervals: list[TimeInterval],
        break_start_local: Optional[str] = None,
        break_end_local: Optional[str] = None,
    ) -> DayAvailabilityDTO:
        tz = ZoneInfo(specialist_timezone)

        work_start_utc = self._local_time_to_utc(target_date, working_start_local, tz)
        work_end_utc = self._local_time_to_utc(target_date, working_end_local, tz)

        all_busy = list(busy_intervals)
        if break_start_local and break_end_local:
            break_start_utc = self._local_time_to_utc(target_date, break_start_local, tz)
            break_end_utc = self._local_time_to_utc(target_date, break_end_local, tz)
            all_busy.append(TimeInterval(start_at=break_start_utc, end_at=break_end_utc))

        effective_duration = timedelta(
            minutes=service_duration_minutes + buffer_after_minutes
        )
        service_duration = timedelta(minutes=service_duration_minutes)
        grid_step = timedelta(minutes=SLOT_GRID_MINUTES)

        now_utc = datetime.now(tz=timezone.utc)
        min_start = now_utc + timedelta(minutes=MIN_BOOKING_AHEAD_MINUTES)

        slots: list[AvailableSlotDTO] = []
        cursor = work_start_utc

        while cursor + effective_duration <= work_end_utc:
            slot_with_buffer = TimeInterval(
                start_at=cursor,
                end_at=cursor + effective_duration,
            )

            if cursor >= min_start:
                is_free = not any(
                    slot_with_buffer.overlaps(busy)
                    for busy in all_busy
                )
                if is_free:
                    local_start = cursor.astimezone(tz)
                    slots.append(AvailableSlotDTO(
                        start_at=cursor,
                        end_at=cursor + service_duration,
                        start_local=local_start.strftime("%H:%M"),
                        duration_minutes=service_duration_minutes,
                    ))

            cursor += grid_step

        return DayAvailabilityDTO(
            date=target_date,
            is_working_day=True,
            slots=slots,
        )

    @staticmethod
    def _local_time_to_utc(target_date: date, time_str: str, tz: ZoneInfo) -> datetime:
        """Converts a local time string ("HH:MM") on a specific date to UTC."""
        hour, minute = map(int, time_str.split(":"))
        local_dt = datetime(
            year=target_date.year,
            month=target_date.month,
            day=target_date.day,
            hour=hour,
            minute=minute,
            tzinfo=tz,
        )
        return local_dt.astimezone(timezone.utc)
