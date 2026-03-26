"""
AvailabilityQueryService — Read-side service for slot availability.

Orchestrates working hours, busy intervals, and slot generation.
Uses cache when available, falls back to live computation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from appointments.application.dto import (
    DayAvailabilityDTO,
    GetAvailabilityDTO,
    GetAvailabilityWeekDTO,
    WeekAvailabilityDTO,
)
from appointments.domain.value_objects import TimeInterval
from appointments.infrastructure.availability.providers import (
    BusyIntervalProvider,
    make_read_provider,
)
from appointments.infrastructure.availability.slot_builder import SlotBuilderService
from appointments.infrastructure.cache.slot_cache import SlotCacheService

logger = logging.getLogger(__name__)


class AvailabilityQueryService:
    """Read-side availability query service with caching."""

    def __init__(
        self,
        busy_provider: BusyIntervalProvider | None = None,
        slot_builder: SlotBuilderService | None = None,
        cache: SlotCacheService | None = None,
    ) -> None:
        self._provider = busy_provider or make_read_provider()
        self._builder = slot_builder or SlotBuilderService()
        self._cache = cache or SlotCacheService()

    def get_day_availability(self, dto: GetAvailabilityDTO) -> DayAvailabilityDTO:
        cached = self._cache.get(dto.specialist_id, dto.target_date, dto.service_id)
        if cached is not None:
            logger.debug(
                "availability.cache_hit specialist=%s date=%s",
                dto.specialist_id, dto.target_date,
            )
            return cached

        result = self._compute_day_availability(
            specialist_id=dto.specialist_id,
            target_date=dto.target_date,
            service_id=dto.service_id,
        )

        self._cache.set(dto.specialist_id, dto.target_date, dto.service_id, result)
        return result

    def get_week_availability(self, dto: GetAvailabilityWeekDTO) -> WeekAvailabilityDTO:
        from users.models import SpecialistProfile
        from services.models import Service

        specialist = SpecialistProfile.objects.get(id=dto.specialist_id)
        service = Service.objects.get(id=dto.service_id)

        days = []
        dates = [dto.start_date + timedelta(days=i) for i in range(dto.days)]

        range_start_utc = self._date_to_utc_start(dates[0], specialist.timezone)
        range_end_utc = self._date_to_utc_end(dates[-1], specialist.timezone)

        all_busy = self._provider.get_busy_intervals(
            specialist_id=dto.specialist_id,
            day_start_utc=range_start_utc,
            day_end_utc=range_end_utc,
        )

        for target_date in dates:
            cached = self._cache.get(dto.specialist_id, target_date, dto.service_id)
            if cached is not None:
                days.append(cached)
                continue

            working_hours = self._get_working_hours(specialist, target_date)
            if working_hours is None:
                day_result = DayAvailabilityDTO(
                    date=target_date, is_working_day=False,
                )
                days.append(day_result)
                continue

            day_start_utc = self._date_to_utc_start(target_date, specialist.timezone)
            day_end_utc = self._date_to_utc_end(target_date, specialist.timezone)

            day_busy = [
                b for b in all_busy
                if b.start_at < day_end_utc and b.end_at > day_start_utc
            ]

            day_result = self._builder.build(
                target_date=target_date,
                specialist_timezone=specialist.timezone,
                working_start_local=working_hours["start"],
                working_end_local=working_hours["end"],
                service_duration_minutes=service.duration_minutes,
                buffer_after_minutes=service.buffer_after_minutes,
                busy_intervals=day_busy,
                break_start_local=working_hours.get("break_start"),
                break_end_local=working_hours.get("break_end"),
            )

            self._cache.set(
                dto.specialist_id, target_date, dto.service_id, day_result,
            )
            days.append(day_result)

        return WeekAvailabilityDTO(
            specialist_id=dto.specialist_id,
            service_id=dto.service_id,
            days=days,
        )

    def _compute_day_availability(
        self,
        specialist_id,
        target_date: date,
        service_id,
    ) -> DayAvailabilityDTO:
        from users.models import SpecialistProfile
        from services.models import Service

        specialist = SpecialistProfile.objects.get(id=specialist_id)
        service = Service.objects.get(id=service_id)

        working_hours = self._get_working_hours(specialist, target_date)
        if working_hours is None:
            return DayAvailabilityDTO(date=target_date, is_working_day=False)

        day_start_utc = self._date_to_utc_start(target_date, specialist.timezone)
        day_end_utc = self._date_to_utc_end(target_date, specialist.timezone)

        busy_intervals = self._provider.get_busy_intervals(
            specialist_id=specialist_id,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
        )

        return self._builder.build(
            target_date=target_date,
            specialist_timezone=specialist.timezone,
            working_start_local=working_hours["start"],
            working_end_local=working_hours["end"],
            service_duration_minutes=service.duration_minutes,
            buffer_after_minutes=service.buffer_after_minutes,
            busy_intervals=busy_intervals,
            break_start_local=working_hours.get("break_start"),
            break_end_local=working_hours.get("break_end"),
        )

    @staticmethod
    def _get_working_hours(specialist, target_date: date) -> dict | None:
        from appointments.models import SpecialistWorkingHours
        day_of_week = target_date.weekday()

        wh = SpecialistWorkingHours.objects.filter(
            specialist=specialist,
            day_of_week=day_of_week,
            is_working_day=True,
        ).first()

        if not wh:
            return None

        return {
            "start": wh.start_time.strftime("%H:%M"),
            "end": wh.end_time.strftime("%H:%M"),
            "break_start": wh.break_start.strftime("%H:%M") if wh.break_start else None,
            "break_end": wh.break_end.strftime("%H:%M") if wh.break_end else None,
        }

    @staticmethod
    def _date_to_utc_start(target_date: date, timezone_str: str) -> datetime:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_str)
        local_midnight = datetime(
            target_date.year, target_date.month, target_date.day,
            0, 0, 0, tzinfo=tz,
        )
        return local_midnight.astimezone(timezone.utc)

    @staticmethod
    def _date_to_utc_end(target_date: date, timezone_str: str) -> datetime:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_str)
        local_end = datetime(
            target_date.year, target_date.month, target_date.day,
            23, 59, 59, 999999, tzinfo=tz,
        )
        return local_end.astimezone(timezone.utc)
