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
from django.db.models import Q, QuerySet

from services.catalog_reads import (
    catalog_services_for,
    catalog_services_prefetch,
)
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
    # OD-1. Категории активной цели клиента, уже разрешённые вызывающей
    # стороной (goals.wiring.goal_category_ids_for). Движок о целях
    # по-прежнему НЕ знает — он получает готовые category_id, как и
    # требует граница из goals/resolution.py.
    #
    # Отдельное поле, а не переиспользование ``category_id``: там один
    # скаляр и явный запрос пользователя, здесь набор (цель курируется
    # на корне и раскрывается вниз до подкатегорий, DRF-1308) и
    # пассивный фон. Смешать их значило бы менять смысл существующего
    # поля и его вклад в ``_score_service_match``.
    #
    # ``None`` — фильтр не применяется (флаг выключен либо цель
    # разрешить нельзя). Кортеж — dataclass frozen и должен остаться
    # хешируемым.
    goal_category_ids: tuple[UUID, ...] | None = None

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
        # Ключ добавляется ТОЛЬКО когда цель применена. Иначе digest
        # каждого запроса изменился бы от одного факта появления поля,
        # и выкладка с выключенным флагом разом обнулила бы кэш — то
        # есть изменила бы поведение, которое обязана оставить прежним.
        #
        # Цель обязана входить в ключ: без этого выдача, отфильтрованная
        # одной целью, досталась бы тому же клиенту после смены цели —
        # и наоборот, прежняя нефильтрованная выдача пережила бы
        # включение флага на весь TTL.
        if self.goal_category_ids:
            payload["goal_category_ids"] = [
                str(category_id) for category_id in self.goal_category_ids
            ]
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
    @staticmethod
    def _goal_category_predicate(category_ids: tuple[UUID, ...]) -> Q:
        """Услуга попадает в цель по своей категории, иначе — по шаблону.

        ``SalonService.category`` обнуляем по схеме (обязателен только
        когда нет ``template``), а ``ServiceTemplate.category`` —
        NOT NULL. Без запасного пути услуга, заведённая от шаблона без
        собственной категории, выпала бы из цели молча.

        Это ФОЛБЭК, а не объединение: своя категория салона побеждает.
        Симметрично ``services.goal_resolution.goals_for_service``,
        которое ходит в обратную сторону и следует тому же решению
        владельца (DRF-1308 п.1 и п.4) — не приписывать услуге цель,
        которой владелец для неё не заявлял. Поэтому шаблон читается
        ТОЛЬКО когда своей категории нет вовсе.

        На пилоте 2026-08-29 категория заполнена у всех 94 салонных
        услуг, то есть сегодня обе ветки дают одно и то же. Условие
        написано по схеме, а не по этому замеру: следующий тенант
        заедет иначе.
        """
        return (
            Q(specialist_services__salon_service__category_id__in=category_ids)
            | Q(
                specialist_services__salon_service__category_id__isnull=True,
                specialist_services__salon_service__template__category_id__in=(
                    category_ids
                ),
            )
        )

    def _fetch_candidates(
        self,
        query: RecommendationQuery,
        min_rating: float,
        limit: int,
    ) -> list[SpecialistProfile]:
        # Порог по рейтингу здесь НЕ применяется — он навешивается
        # ниже, отдельно на «мастеров с оценками». См. DRF-1433 и
        # докстринг ``_split_by_review_evidence``.
        qs = SpecialistProfile.objects.filter(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
            is_available=True,
            is_booking_enabled=True,
        )

        # DRF-1430. Отключённый салон уводит своих мастеров из выдачи.
        #
        # До этого фильтра движок спрашивал только про
        # ``SpecialistProfile`` и таблицу салонов не соединял вовсе.
        # ``Tenant.is_active=False`` прятал салон от того, кто
        # спрашивает про салоны (дефолтный ``_ActiveTenantManager``), а
        # мастер этого салона попадал в подбор нетронутым. То есть
        # отключение салона не было средством убрать его из выдачи.
        #
        # Почему ``is_active``, а не «одно из трёх полей»: у ``Tenant``
        # поле состояния РОВНО ОДНО (id, slug, name, is_active,
        # created_at, updated_at). ``status`` и ``is_booking_enabled``
        # — поля ``SpecialistProfile``, они про мастера, и они уже
        # прочитаны строками выше. Салонное состояние в этой схеме
        # выражается единственным флагом.
        #
        # Почему НЕ ``filter(tenant__is_active=True)``:
        # ``SpecialistProfile.tenant`` — ``null=True`` (бэкфилл
        # DRF-242.4 не закрыт), и такой фильтр дал бы INNER JOIN,
        # молча выкосив КАЖДЫЙ профиль без салона. ``OR`` с
        # ``isnull=True`` заставляет планировщик взять LEFT JOIN и
        # оставляет их на месте: тикет просит, чтобы состояние салона
        # влияло на выдачу, а не чтобы наличие салона стало новым
        # требованием к мастеру.
        #
        # Salon-to-master — many-to-one, размножения строк нет, поэтому
        # ``distinct()`` здесь не нужен (в отличие от фильтров по
        # услугам ниже).
        qs = qs.filter(Q(tenant__isnull=True) | Q(tenant__is_active=True))

        if query.city:
            qs = qs.filter(address__icontains=query.city)

        if query.category_id:
            qs = qs.filter(
                services__category_id=query.category_id,
                services__is_active=True,
            ).distinct()

        # OD-1. Цель клиента — жёсткий фильтр, как и явная категория.
        # В ``_score_service_match`` она сознательно НЕ участвует:
        # цель уже сузила пул, а добавь мы её ещё и в 15%-й вес, при
        # выключенном флаге ранжирование осталось бы прежним только
        # случайно. Один эффект — одно место.
        #
        # Фильтр идёт по КАНОНИЧЕСКОМУ каталогу
        # (``SpecialistService`` -> ``SalonService``), а НЕ по легаси
        # ``Service`` рядом строкой выше. Замер пилота 2026-08-29:
        # SpecialistService 292, SalonService 94, легаси Service — 0.
        # Фильтр по легаси отдал бы пустую полку каждому, кто выбрал
        # цель. Легаси-ветки (``category_id``/``price_max``) не
        # трогаем: их перевод — отдельный чанк S3-CUT.
        if query.goal_category_ids:
            qs = qs.filter(
                self._goal_category_predicate(query.goal_category_ids),
                specialist_services__is_active=True,
                specialist_services__salon_service__is_active=True,
            ).distinct()

        if query.price_max is not None:
            qs = qs.filter(
                services__price__lte=query.price_max,
                services__is_active=True,
            ).distinct()

        return self._split_by_review_evidence(qs, min_rating, limit)

    @staticmethod
    def _split_by_review_evidence(
        qs: QuerySet[SpecialistProfile],
        min_rating: float,
        limit: int,
    ) -> list[SpecialistProfile]:
        """Две непересекающиеся выборки: «оценён» и «не оценён».

        DRF-1433. Порог по рейтингу — защита от ПЛОХИХ оценок, а не от
        их отсутствия. До этой правки оба состояния выглядели в базе
        одинаково (``rating = 0.0``), и ``rating__gte=min_rating``
        рубил их вместе: на боевом пилоте 31.08 у всех девяти мастеров
        ``reviews_count = 0``, и подбор возвращал ноль кандидатов —
        а значит ноль по всем семи целям в ``goal_master_coverage()``.
        Выйти из этого мастер не мог: рейтинг берётся из отзывов, отзыв
        — после визита, визит — после записи, запись — после подбора.

        Различает состояния ``reviews_count``:
        ``reviews.views._recalculate_rating`` пересчитывает его как
        ``Count`` неспрятанных отзывов, то есть ``reviews_count == 0``
        означает ровно «оценок нет вовсе», а не «оценки плохие».
        Мастер с плохими оценками (``reviews_count > 0`` и рейтинг ниже
        порога) отсекается ровно как раньше.

        Почему ДВА запроса, а не один ``Q(...) | Q(reviews_count=0)``.
        Headroom ``[: limit * 3]`` берётся по ``-rating``, где мастер
        без оценок стоит последним — ниже любого оценённого. Одним
        запросом порог был бы снят, но на каталоге больше ``limit * 3``
        новичок просто не доходил бы до скоринга, и гарантия молча
        исчезала бы по мере роста каталога. Отдельный headroom на
        каждую группу этого не допускает; верхняя граница выборки
        удваивается с ``3 * limit`` до ``6 * limit`` — ранжирование
        всё равно делает скоринг, а не этот срез.

        Порядок конкатенации (сначала оценённые) — это тай-брейк:
        ``sort`` в ``_compute`` стабилен, поэтому при РАВНОМ composite
        впереди останется мастер с подтверждёнными отзывами.
        """
        prefetch = catalog_services_prefetch()
        reviewed = (
            qs.filter(reviews_count__gt=0, rating__gte=min_rating)
            .order_by("-rating", "-reviews_count", "id")[: limit * 3]
            .prefetch_related(*prefetch)
        )
        # У группы без отзывов ``rating`` ничего не значит (0.0 у
        # живого мастера, выдуманное число у демо-каталога из #272),
        # поэтому сортировать по нему нельзя — только стабильный ``id``
        # ради воспроизводимости выборки между репликами.
        unreviewed = (
            qs.filter(reviews_count=0)
            .order_by("id")[: limit * 3]
            .prefetch_related(*prefetch)
        )
        return [*reviewed, *unreviewed]

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

        DRF-1433: при ``reviews_count == 0`` насыщение равно нулю, и
        весь 30%-й вклад рейтинга обнуляется — независимо от значения
        в поле ``rating``. Поэтому мастер без отзывов, которого порог
        больше не отсекает, при прочих равных стоит НИЖЕ мастера с
        хорошими отзывами и конкурирует оставшимися 70% (расстояние,
        доступность, совпадение услуг, история). Отдельного правила
        для этого не нужно — оно уже здесь.
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
        # ОБА слоя каталога: легаси ``Service`` на пилоте пуст целиком
        # (замер 2026-08-30: 0 строк против 292 канонических связок), и
        # превью услуг молча приходило пустым везде, где его показывают —
        # полка «рядом с вами» на главной, карточки мастеров в чате Ayla,
        # системный промпт LLM. См. ``services.catalog_reads``.
        services = [
            svc.name for svc in catalog_services_for(s)
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
