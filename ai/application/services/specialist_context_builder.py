"""SpecialistContextBuilder — load top-N candidate specialists for the LLM prompt.

Thin adapter over ``RecommendationEngine`` (DRF-105) — kept for backward
compatibility with the AI Chat call sites that don't need the full
weighted scoring API. The chat layer treats the recommended candidates
as a flat candidate set; ranking quality comes from the engine.

Filter chain (per spec v2.0 §AI ASSISTANT):
  status=active + is_available + is_booking_enabled
  + rating >= AI_SPECIALIST_MIN_RATING — но ТОЛЬКО для мастеров с
    отзывами; ``reviews_count == 0`` порог не отсекает (DRF-1433)
  + (optional) within ~25 km of client location
  weighted by RecommendationEngine
  LIMIT AI_SPECIALIST_CONTEXT_LIMIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings

from ai.application.services.recommendation_engine import (
    RecommendationEngine,
    RecommendationQuery,
    ScoredSpecialist,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


# Backward-compat DTO — chat_service & tools_handlers use this shape.
@dataclass(frozen=True)
class SpecialistCandidate:
    id: UUID
    display_name: str
    rating: Decimal
    reviews_count: int
    address: str
    distance_km: float | None
    services_preview: list[str]
    # Newer fields surfaced from the engine — optional so older callers
    # that construct candidates by hand keep working.
    score: float = 0.0
    match_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_scored(cls, s: ScoredSpecialist) -> "SpecialistCandidate":
        return cls(
            id=s.id,
            display_name=s.display_name,
            rating=s.rating,
            reviews_count=s.reviews_count,
            address=s.address,
            distance_km=s.distance_km,
            services_preview=s.services_preview,
            score=s.score,
            match_reasons=s.match_reasons,
        )


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
            score_hint = f" | score={c.score:.2f}" if c.score > 0 else ""
            lines.append(
                f"- {c.id} | {c.display_name} | ★{c.rating} "
                f"({c.reviews_count} отз.){distance}{services}{score_hint}"
            )
        return "\n".join(lines)


class SpecialistContextBuilder:
    """Builds candidate specialist list for the LLM system prompt.

    Thin wrapper around RecommendationEngine. Caching, scoring, and
    filtering live in the engine; this layer only translates between
    chat-side and engine-side shapes.
    """

    def __init__(
        self,
        *,
        limit: int | None = None,
        min_rating: float | None = None,
        engine: RecommendationEngine | None = None,
    ) -> None:
        self._limit = limit or settings.AI_SPECIALIST_CONTEXT_LIMIT
        self._min_rating = (
            min_rating if min_rating is not None else settings.AI_SPECIALIST_MIN_RATING
        )
        self._engine = engine or RecommendationEngine()

    def build(
        self,
        *,
        client_id: UUID | None = None,
        client_lat: float | None = None,
        client_lon: float | None = None,
        city: str | None = None,
    ) -> SpecialistContext:
        query = RecommendationQuery(
            client_id=client_id,
            client_lat=client_lat,
            client_lon=client_lon,
            city=city,
            min_rating=self._min_rating,
            limit=self._limit,
        )
        result = self._engine.recommend(query)
        return SpecialistContext(
            candidates=[
                SpecialistCandidate.from_scored(s) for s in result.candidates
            ]
        )
