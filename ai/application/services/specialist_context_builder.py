"""SpecialistContextBuilder — load top-N candidate specialists for the LLM prompt.

Spec v2.0 §AI ASSISTANT requires specialists to be limited to the active
catalog. We pull top-N by rating with optional distance ordering and
hand the LLM a compact summary so it can pick from real IDs only.

Filter chain:
  status=active + is_available=True + is_booking_enabled=True
  + rating >= AI_SPECIALIST_MIN_RATING (default 4.0)
  + (optional) within ~25 km of client location
  ORDER BY rating DESC, reviews_count DESC
  LIMIT AI_SPECIALIST_CONTEXT_LIMIT (default 20)

Returns a structured DTO. The LLM gets a short summary string; the
view layer keeps the full IDs for tool_handlers to validate against.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Iterable
from uuid import UUID

from django.conf import settings

from users.models import SpecialistProfile


@dataclass(frozen=True)
class SpecialistCandidate:
    id: UUID
    display_name: str
    rating: Decimal
    reviews_count: int
    address: str
    distance_km: float | None
    services_preview: list[str]


@dataclass(frozen=True)
class SpecialistContext:
    candidates: list[SpecialistCandidate] = field(default_factory=list)

    @property
    def candidate_ids(self) -> set[UUID]:
        return {c.id for c in self.candidates}

    def to_prompt_summary(self) -> str:
        """Compact one-line-per-specialist summary for the system prompt."""
        if not self.candidates:
            return "(нет доступных мастеров под фильтр)"
        lines = []
        for c in self.candidates:
            distance = (
                f", {c.distance_km:.1f} км" if c.distance_km is not None else ""
            )
            services = (
                f" — {', '.join(c.services_preview[:3])}"
                if c.services_preview
                else ""
            )
            lines.append(
                f"- {c.id} | {c.display_name} | ★{c.rating} "
                f"({c.reviews_count} отз.){distance}{services}"
            )
        return "\n".join(lines)


def _haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in km. Used only for ordering, not display."""
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))


class SpecialistContextBuilder:
    """Builds candidate specialist list for the LLM system prompt.

    Cheap call (single ORM query + Python sort). Safe to run on every
    chat turn — caching deferred until usage shows it as hot path.
    """

    def __init__(
        self,
        *,
        limit: int | None = None,
        min_rating: float | None = None,
    ) -> None:
        self._limit = limit or settings.AI_SPECIALIST_CONTEXT_LIMIT
        self._min_rating = (
            min_rating if min_rating is not None else settings.AI_SPECIALIST_MIN_RATING
        )

    def build(
        self,
        *,
        client_lat: float | None = None,
        client_lon: float | None = None,
    ) -> SpecialistContext:
        qs = SpecialistProfile.objects.filter(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
            is_available=True,
            is_booking_enabled=True,
            rating__gte=self._min_rating,
        ).order_by("-rating", "-reviews_count")[: self._limit * 2]
        # Pull 2x limit so we have room to re-sort by distance without
        # going back to the DB.

        candidates = list(qs)
        scored = self._score_with_distance(candidates, client_lat, client_lon)
        scored.sort(key=self._sort_key)
        top = scored[: self._limit]
        return SpecialistContext(
            candidates=[self._to_candidate(s, d) for s, d in top]
        )

    @staticmethod
    def _score_with_distance(
        specialists: Iterable[SpecialistProfile],
        client_lat: float | None,
        client_lon: float | None,
    ) -> list[tuple[SpecialistProfile, float | None]]:
        out: list[tuple[SpecialistProfile, float | None]] = []
        for s in specialists:
            distance: float | None = None
            if (
                client_lat is not None
                and client_lon is not None
                and s.location_lat is not None
                and s.location_lng is not None
            ):
                distance = _haversine_km(
                    client_lat,
                    client_lon,
                    float(s.location_lat),
                    float(s.location_lng),
                )
            out.append((s, distance))
        return out

    @staticmethod
    def _sort_key(item: tuple[SpecialistProfile, float | None]) -> tuple:
        s, d = item
        # Ordering: distance ascending (None last), then rating desc, reviews desc.
        return (
            0 if d is not None else 1,
            d if d is not None else 0.0,
            -float(s.rating),
            -s.reviews_count,
        )

    @staticmethod
    def _to_candidate(
        s: SpecialistProfile, distance_km: float | None
    ) -> SpecialistCandidate:
        services = [
            svc.name
            for svc in s.services.filter(is_active=True).order_by("price")[:3]
        ] if hasattr(s, "services") else []
        return SpecialistCandidate(
            id=s.id,
            display_name=s.display_name,
            rating=s.rating,
            reviews_count=s.reviews_count,
            address=s.address,
            distance_km=distance_km,
            services_preview=services,
        )
