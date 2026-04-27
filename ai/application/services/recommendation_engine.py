"""RecommendationEngine — weighted multi-factor specialist ranking.

Per DRF-105 / M3. Scoring model:

  rating          30%
  distance        25%
  availability    20%
  service match   15%
  client history  10%

Each component returns 0.0-1.0; final score is the weighted sum, also
0.0-1.0. Higher = better recommendation.

Designed to be **the** ranker. ``SpecialistContextBuilder`` (used by AI
chat to pick candidates for the LLM prompt) delegates here. The
recommendation API endpoint (Phase 6, when added) will also call this.

## Caching

Cache-aside via Django default cache. Key: ``ai:recs:{client_or_anon}:{filter_hash}``.
TTL 5 min (`AI_REC_CACHE_TTL` env). Bypass with ``use_cache=False`` for tests.

## Availability score caveat

True slot count per specialist would call ``AvailabilityQueryService``
N times — too expensive for a single recommendation pass. MVP uses a
**cheap proxy**: ``is_booking_enabled`` (binary) + presence of
``working_hours`` rows for the upcoming week. Real slot integration
is a Phase 6 follow-up — flagged via TODO in ``_score_availability``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Iterable
from uuid import UUID

from django.conf import settings
from django.core.cache import cache as default_cache

from users.models import SpecialistProfile

logger = logging.getLogger(__name__)


# --- Scoring weights — DRF-105 contract --------------------------------------

WEIGHT_RATING = 0.30
WEIGHT_DISTANCE = 0.25
WEIGHT_AVAILABILITY = 0.20
WEIGHT_SERVICE_MATCH = 0.15
WEIGHT_HISTORY = 0.10

# Sanity check: weights must sum to 1.0 (within float tolerance) so the
# composite score stays in [0, 1].
_TOTAL_WEIGHT = (
    WEIGHT_RATING + WEIGHT_DISTANCE + WEIGHT_AVAILABILITY
    + WEIGHT_SERVICE_MATCH + WEIGHT_HISTORY
)
assert abs(_TOTAL_WEIGHT - 1.0) < 1e-9, f"weights must sum to 1.0, got {_TOTAL_WEIGHT}"


# Distance saturates at this radius — anything farther scores 0.
DEFAULT_MAX_DISTANCE_KM = 25.0

# Reviews-count saturation: fresh masters with 5★ but 2 reviews shouldn't
# beat established 4.7★ with 80 reviews. Saturation point — when review
# count hits this, the rating sub-score effectively trusts the average.
REVIEWS_SATURATION = 10


# --- DTOs --------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationQuery:
    """Filter input. Hashable for cache key derivation."""

    client_id: UUID | None = None  # None for anonymous
    client_lat: float | None = None
    client_lon: float | None = None
    city: str | None = None  # client's city for soft city match
    category_id: UUID | None = None
    price_max: Decimal | None = None
    min_rating: float | None = None  # falls back to settings.AI_SPECIALIST_MIN_RATING
    limit: int | None = None  # falls back to settings.AI_SPECIALIST_CONTEXT_LIMIT

    def cache_key(self) -> str:
        payload = {
            "client_id": str(self.client_id) if self.client_id else "anon",
            "lat": round(self.client_lat, 3) if self.client_lat is not None else None,
            "lon": round(self.client_lon, 3) if self.client_lon is not None else None,
            "city": (self.city or "").lower(),
            "category_id": str(self.category_id) if self.category_id else None,
            "price_max": str(self.price_max) if self.price_max is not None else None,
            "min_rating": self.min_rating,
            "limit": self.limit,
        }
        digest = hashlib.sha1(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
        ).hexdigest()[:16]
        owner = str(self.client_id) if self.client_id else "anon"
        return f"ai:recs:{owner}:{digest}"


@dataclass(frozen=True)
class ScoreBreakdown:
    """Why a specialist scored what it did. Surfaced in tool action_data
    so the LLM can quote reasons (`match_reasons`) honestly."""

    rating: float
    distance: float
    availability: float
    service_match: float
    history: float

    @property
    def composite(self) -> float:
        return (
            WEIGHT_RATING * self.rating
            + WEIGHT_DISTANCE * self.distance
            + WEIGHT_AVAILABILITY * self.availability
            + WEIGHT_SERVICE_MATCH * self.service_match
            + WEIGHT_HISTORY * self.history
        )

    def top_reasons(self) -> list[str]:
        """Human-readable top contributors. Caller can show 1-3 reasons
        per specialist in UI."""
        items = [
            ("Высокий рейтинг", self.rating * WEIGHT_RATING),
            ("Близко", self.distance * WEIGHT_DISTANCE),
            ("Свободные слоты", self.availability * WEIGHT_AVAILABILITY),
            ("Подходящие услуги", self.service_match * WEIGHT_SERVICE_MATCH),
            ("Уже записывались", self.history * WEIGHT_HISTORY),
        ]
        items.sort(key=lambda x: -x[1])
        return [name for name, contribution in items[:3] if contribution > 0.05]


@dataclass(frozen=True)
class ScoredSpecialist:
    id: UUID
    display_name: str
    rating: Decimal
    reviews_count: int
    address: str
    distance_km: float | None
    services_preview: list[str]
    score: float                  # 0.0-1.0
    breakdown: ScoreBreakdown
    match_reasons: list[str]


@dataclass(frozen=True)
class RecommendationResult:
    candidates: list[ScoredSpecialist] = field(default_factory=list)

    @property
    def candidate_ids(self) -> set[UUID]:
        return {c.id for c in self.candidates}

    def to_prompt_summary(self) -> str:
        """Compact one-line-per-specialist summary for LLM system prompt."""
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
                f"({c.reviews_count} отз.){distance}{services} | "
                f"score={c.score:.2f}"
            )
        return "\n".join(lines)


# --- Engine ------------------------------------------------------------------


class RecommendationEngine:
    """Weighted multi-factor specialist ranker with cache-aside.

    Stateless — safe to instantiate per request. All heavy lookups are
    behind ``cache`` and ``__init__`` accepts overrides for testing.
    """

    def __init__(
        self,
        *,
        cache=None,
        max_distance_km: float = DEFAULT_MAX_DISTANCE_KM,
    ) -> None:
        self._cache = cache or default_cache
        self._max_distance_km = max_distance_km

    def recommend(
        self,
        query: RecommendationQuery,
        *,
        use_cache: bool = True,
    ) -> RecommendationResult:
        if use_cache:
            cached = self._cache.get(query.cache_key())
            if cached is not None:
                logger.debug("ai.recommend.cache_hit key=%s", query.cache_key())
                return cached

        result = self._compute(query)

        if use_cache:
            ttl = getattr(settings, "AI_REC_CACHE_TTL", 300)
            self._cache.set(query.cache_key(), result, timeout=ttl)
        return result

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _compute(self, query: RecommendationQuery) -> RecommendationResult:
        limit = query.limit or settings.AI_SPECIALIST_CONTEXT_LIMIT
        min_rating = (
            query.min_rating
            if query.min_rating is not None
            else settings.AI_SPECIALIST_MIN_RATING
        )

        candidates = self._fetch_candidates(query, min_rating, limit)
        history_set = self._load_history_specialist_ids(query.client_id)
        history_categories = self._load_history_category_ids(query.client_id)

        scored = []
        for s in candidates:
            distance = self._distance_to(s, query)
            breakdown = ScoreBreakdown(
                rating=self._score_rating(s),
                distance=self._score_distance(distance),
                availability=self._score_availability(s),
                service_match=self._score_service_match(s, query),
                history=self._score_history(s, history_set, history_categories),
            )
            scored.append((s, distance, breakdown))

        scored.sort(key=lambda item: -item[2].composite)
        top = scored[:limit]

        return RecommendationResult(
            candidates=[self._to_scored(s, d, b) for s, d, b in top]
        )

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def _fetch_candidates(
        self,
        query: RecommendationQuery,
        min_rating: float,
        limit: int,
    ) -> list[SpecialistProfile]:
        qs = SpecialistProfile.objects.filter(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
            is_available=True,
            is_booking_enabled=True,
            rating__gte=min_rating,
        )

        if query.city:
            qs = qs.filter(address__icontains=query.city)

        if query.category_id:
            qs = qs.filter(
                services__category_id=query.category_id,
                services__is_active=True,
            ).distinct()

        if query.price_max is not None:
            qs = qs.filter(
                services__price__lte=query.price_max,
                services__is_active=True,
            ).distinct()

        # Pull ~3x limit so the scorer has headroom — cheaper than
        # paginating per filter combo.
        qs = qs.order_by("-rating", "-reviews_count")[: limit * 3]
        return list(qs.prefetch_related("services"))

    def _load_history_specialist_ids(self, client_id: UUID | None) -> set[UUID]:
        if client_id is None:
            return set()
        from appointments.models import Appointment

        return set(
            Appointment.objects.filter(
                client_id=client_id,
                status=Appointment.Status.COMPLETED,
            ).values_list("specialist_id", flat=True)
        )

    def _load_history_category_ids(self, client_id: UUID | None) -> set[UUID]:
        if client_id is None:
            return set()
        from appointments.models import Appointment

        return set(
            Appointment.objects.filter(
                client_id=client_id,
                status=Appointment.Status.COMPLETED,
                service__category__isnull=False,
            ).values_list("service__category_id", flat=True)
        )

    # ------------------------------------------------------------------
    # scoring (each returns 0.0 - 1.0)
    # ------------------------------------------------------------------
    @staticmethod
    def _score_rating(s: SpecialistProfile) -> float:
        """Rating scaled to [0, 1] with reviews-count saturation.

        Pure rating: (rating - 1) / 4 → 1★=0, 5★=1.
        Saturation: reviews_count / (reviews_count + REVIEWS_SATURATION).
        Composite: pure_rating × saturation. Fresh masters with
        few reviews but high rating still surface but not at full weight.
        """
        if s.rating is None:
            return 0.0
        pure = max(0.0, (float(s.rating) - 1.0) / 4.0)
        saturation = (
            s.reviews_count / (s.reviews_count + REVIEWS_SATURATION)
            if s.reviews_count >= 0 else 0.0
        )
        return pure * saturation

    def _score_distance(self, distance_km: float | None) -> float:
        """Linear decay from 1.0 at 0km to 0.0 at max_distance_km.

        ``None`` (no client geo OR no specialist geo) → 0.5 — neutral
        contribution; we don't penalise specialists for missing
        location data because client without geo can't punish either.
        """
        if distance_km is None:
            return 0.5
        if distance_km >= self._max_distance_km:
            return 0.0
        return 1.0 - (distance_km / self._max_distance_km)

    @staticmethod
    def _score_availability(s: SpecialistProfile) -> float:
        """MVP proxy: binary on ``is_booking_enabled``.

        Real slot count would mean N calls to AvailabilityQueryService —
        too expensive on the recommendation hot path. Phase 6 follow-up:
        precompute ``has_slots_next_7d`` flag via Celery beat and read here.
        """
        # All candidates are pre-filtered by is_booking_enabled=True,
        # so this currently returns 1.0 for everyone. Kept as separate
        # sub-score so the Phase 6 upgrade has a single place to land
        # without reshaping the scoring model.
        return 1.0

    @staticmethod
    def _score_service_match(
        s: SpecialistProfile, query: RecommendationQuery,
    ) -> float:
        """Hard match on category + soft match on price ceiling.

        - No filter: 1.0 (no preference signal).
        - Has services in target category, fits price: 1.0
        - Has services in target category, none fit price: 0.5
        - Has fitting price but no category match: 0.6
        - Neither: 0.0
        """
        if query.category_id is None and query.price_max is None:
            return 1.0

        active_services = [
            svc for svc in s.services.all() if svc.is_active
        ]
        category_match = (
            any(svc.category_id == query.category_id for svc in active_services)
            if query.category_id else None
        )
        price_match = (
            any(svc.price <= query.price_max for svc in active_services)
            if query.price_max is not None else None
        )

        if category_match is not None and price_match is not None:
            if category_match and price_match:
                # Best case — has services in category at acceptable price.
                in_cat_in_price = any(
                    svc.category_id == query.category_id
                    and svc.price <= query.price_max
                    for svc in active_services
                )
                return 1.0 if in_cat_in_price else 0.7
            if category_match:
                return 0.5
            if price_match:
                return 0.4
            return 0.0
        if category_match is not None:
            return 1.0 if category_match else 0.0
        if price_match is not None:
            return 0.6 if price_match else 0.0
        return 1.0

    @staticmethod
    def _score_history(
        s: SpecialistProfile,
        history_specialist_ids: set[UUID],
        history_category_ids: set[UUID],
    ) -> float:
        """Returning-client boost.

        - Past completed appointment with this specialist → 1.0
        - Past completed appointment in a category this specialist serves → 0.5
        - First-time client → 0.0
        """
        if s.id in history_specialist_ids:
            return 1.0
        if not history_category_ids:
            return 0.0
        # specialist serves a category the client used before
        try:
            specialist_categories = {
                svc.category_id for svc in s.services.all()
                if svc.is_active and svc.category_id is not None
            }
        except (AttributeError, ValueError):
            return 0.0
        if specialist_categories & history_category_ids:
            return 0.5
        return 0.0

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _distance_to(
        s: SpecialistProfile, query: RecommendationQuery,
    ) -> float | None:
        if (
            query.client_lat is None or query.client_lon is None
            or s.location_lat is None or s.location_lng is None
        ):
            return None
        return _haversine_km(
            query.client_lat, query.client_lon,
            float(s.location_lat), float(s.location_lng),
        )

    @staticmethod
    def _to_scored(
        s: SpecialistProfile,
        distance_km: float | None,
        breakdown: ScoreBreakdown,
    ) -> ScoredSpecialist:
        services = [
            svc.name for svc in s.services.all()
            if svc.is_active
        ][:3]
        return ScoredSpecialist(
            id=s.id,
            display_name=s.display_name,
            rating=s.rating,
            reviews_count=s.reviews_count,
            address=s.address,
            distance_km=distance_km,
            services_preview=services,
            score=breakdown.composite,
            breakdown=breakdown,
            match_reasons=breakdown.top_reasons(),
        )


# --- Math --------------------------------------------------------------------


def _haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))


def _iter_active_services(
    specialists: Iterable[SpecialistProfile],
) -> Iterable[tuple[SpecialistProfile, list]]:
    """Helper for tests/admin — yields (specialist, [active services])."""
    for s in specialists:
        yield s, [svc for svc in s.services.all() if svc.is_active]
