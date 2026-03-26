"""
SlotCacheService — availability cache using Django cache framework.

For MVP uses LocMemCache. Ready for Redis (django-redis) in production.
Cache is best-effort: errors are logged, not raised.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional
from uuid import UUID

from django.core.cache import cache

from appointments.application.dto import AvailableSlotDTO, DayAvailabilityDTO

logger = logging.getLogger(__name__)

SLOTS_CACHE_TTL_SECONDS = 60
INDEX_CACHE_TTL_SECONDS = 300


class SlotCacheService:
    """Read-through cache for availability slots."""

    def get(
        self,
        specialist_id: UUID,
        target_date: date,
        service_id: UUID,
    ) -> Optional[DayAvailabilityDTO]:
        try:
            key = self._slot_key(specialist_id, target_date, service_id)
            raw = cache.get(key)
            if raw is None:
                return None
            return self._deserialize(raw)
        except Exception:
            logger.warning(
                "slot_cache.get_error specialist_id=%s date=%s",
                specialist_id, target_date, exc_info=True,
            )
            return None

    def set(
        self,
        specialist_id: UUID,
        target_date: date,
        service_id: UUID,
        availability: DayAvailabilityDTO,
    ) -> None:
        try:
            key = self._slot_key(specialist_id, target_date, service_id)
            cache.set(key, self._serialize(availability), timeout=SLOTS_CACHE_TTL_SECONDS)

            index_key = self._index_key(specialist_id, target_date)
            existing_index: set = cache.get(index_key) or set()
            existing_index.add(str(service_id))
            cache.set(index_key, existing_index, timeout=INDEX_CACHE_TTL_SECONDS)
        except Exception:
            logger.warning(
                "slot_cache.set_error specialist_id=%s date=%s",
                specialist_id, target_date, exc_info=True,
            )

    def invalidate(self, specialist_id: UUID, target_date: date) -> None:
        try:
            index_key = self._index_key(specialist_id, target_date)
            service_ids: set = cache.get(index_key) or set()

            if service_ids:
                keys_to_delete = [
                    self._slot_key(specialist_id, target_date, sid)
                    for sid in service_ids
                ]
                cache.delete_many(keys_to_delete)

            cache.delete(index_key)
        except Exception:
            logger.warning(
                "slot_cache.invalidate_error specialist_id=%s date=%s",
                specialist_id, target_date, exc_info=True,
            )

    @staticmethod
    def _slot_key(specialist_id, target_date: date, service_id) -> str:
        return f"slots:v1:{specialist_id}:{target_date.isoformat()}:{service_id}"

    @staticmethod
    def _index_key(specialist_id, target_date: date) -> str:
        return f"slots_index:v1:{specialist_id}:{target_date.isoformat()}"

    @staticmethod
    def _serialize(availability: DayAvailabilityDTO) -> str:
        return json.dumps({
            "date": availability.date.isoformat(),
            "is_working_day": availability.is_working_day,
            "slots": [
                {
                    "start_at": s.start_at.isoformat(),
                    "end_at": s.end_at.isoformat(),
                    "start_local": s.start_local,
                    "duration_minutes": s.duration_minutes,
                }
                for s in availability.slots
            ],
        })

    @staticmethod
    def _deserialize(raw: str) -> DayAvailabilityDTO:
        data = json.loads(raw)
        return DayAvailabilityDTO(
            date=date.fromisoformat(data["date"]),
            is_working_day=data["is_working_day"],
            slots=[
                AvailableSlotDTO(
                    start_at=datetime.fromisoformat(s["start_at"]),
                    end_at=datetime.fromisoformat(s["end_at"]),
                    start_local=s["start_local"],
                    duration_minutes=s["duration_minutes"],
                )
                for s in data["slots"]
            ],
        )
