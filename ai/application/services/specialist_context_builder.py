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
from enum import StrEnum
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


class OrderProvenance(StrEnum):
    """Чей это порядок. Обязателен, потому что сохранённость его не доказывает.

    Решение владельца В-16, дословно: «LLM не переставляла кандидатов» —
    **недостаточно**. Потребитель вправе сохранять смысловой порядок
    только когда происхождение порядка называет канонический
    Recommendation Authority. **Порядок легаси-движка, сохранённый
    идеально, остаётся порядком легаси-движка.**

    До этого поля защита выглядела так: `handle_show_specialists`
    восстанавливал позицию из контекста и объяснял в комментарии, что
    порядок не вправе выбирать модель (`LLM_FORBIDDEN`). Намерение
    верное, результат — **защита от неверного авторитета, построенная
    поверх другого неверного авторитета**: восстанавливался порядок
    движка.

    Намерение в комментарии проверить нечем. Происхождение в данных —
    можно, и потребитель обязан его спросить.
    """

    #: Порядок назвал канонический резолвер. Только он смысловой, и
    #: только его потребитель вправе сохранять.
    CANONICAL_RESOLVER = "CANONICAL_RESOLVER"
    #: Порядок посчитал легаси-движок. Он retrieval, а не авторитет
    #: (DRF-1628): сохранять его как смысловой запрещено.
    LEGACY_ENGINE = "LEGACY_ENGINE"
    #: Порядок несемантичен по построению — алфавит, идентификатор.
    #: Сохранять можно: сохранять нечего.
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class SpecialistContext:
    #: Без умолчания НАМЕРЕННО. Поле с умолчанием можно не заметить, и
    #: следующий производитель контекста промолчит о происхождении
    #: ровно так же, как молчал прежний. Обязательность — единственное,
    #: что заставляет назвать источник в момент создания.
    order_provenance: OrderProvenance
    candidates: list[SpecialistCandidate] = field(default_factory=list)

    @property
    def candidate_ids(self) -> set[UUID]:
        return {c.id for c in self.candidates}

    # `to_prompt_summary()` снят (DRF-1630): он клал в промпт ★рейтинг и
    # расстояние в порядке движка. Вызовов у него не было ни одного — и
    # именно поэтому он опасен: готовый метод это приглашение подключить
    # его снова. Владелец назвал находку классом дефекта, а не багом.


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
            # Порядок здесь считает движок — и это законно, он retrieval.
            # Незаконно было бы промолчать об этом: потребитель, не
            # спросивший происхождения, сохранил бы его как смысловой.
            order_provenance=OrderProvenance.LEGACY_ENGINE,
            candidates=[
                SpecialistCandidate.from_scored(s) for s in result.candidates
            ],
        )
