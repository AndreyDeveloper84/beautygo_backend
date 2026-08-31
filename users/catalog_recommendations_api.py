"""POST /api/v1/internal/me/catalog/recommendations/ — task #99.

W1 booking flow Phase B unblock. Three-layer catalog recommendations
per Tau's §10.1 (project_ayla_ranking_philosophy):

  layer_1_your_places  — specialists in tenants the customer already
                         has an active CUSTOMER-role TUR with (the
                         "salons you know" rail in the Mini App).
  layer_2_ayla_picks   — top-3 specialists NOT in customer's history,
                         ranked by simple composite score with a
                         template reasoning_text per item.
  layer_3_explore      — category-aggregate counts across the
                         eligible pool ("there are 12 manicure
                         specialists, 8 massage" — feeds the
                         "browse by category" UI).

Identity bridging: ``IsBotServiceWithVerifiedClient`` (Bearer +
X-External-User-ID) per memory ``project_identity_bridging_pattern``.
Mini App calls bot-platform proxy → bot-platform calls this endpoint
with the resolved bot_user identity; Ayla resolves to a canonical
``User`` and reads their TUR history backend-side.

Pilot scope discipline:
- NO LLM-generated reasoning text. Template strings only (founder
  pilot_scope_discipline).
- NO availability/slot computation in reasoning (would multiply DB
  load by the size of the candidate pool). The "available" claim is
  reduced to the ``is_available + is_booking_enabled`` boolean pair.
- Minimum eligibility filter only (active specialist + active tenant)
  — no rating/reviews thresholds in MVP per founder cut "simple
  eligibility".
- Goal matching for the request-supplied ``goal`` string is ILIKE on
  ``service.name`` OR ``category.slug``; semantic match is post-pilot.

OD-1 (2026-08-29) — сохранённая цель клиента
--------------------------------------------
Когда запрос НЕ несёт ``goal``, полки 2 и 3 фильтруются категориями
активной цели клиента (``goals.resolution`` через ``goals.wiring``), за
флагом ``GOAL_RESOLUTION_ENABLED``. Это знание, курируемое владельцем в
``GoalOptionCategory``, а не ILIKE по словам.

Фильтр цели идёт по КАНОНИЧЕСКОМУ каталогу (``SpecialistService`` ->
``SalonService``), потому что легаси ``Service`` на пилоте пуст целиком
(замер 2026-08-29: 0 строк против 292 канонических связок).

S3-EMPTY (2026-08-30) — остальная машинерия переведена на оба слоя
------------------------------------------------------------------
Замер пилота показал, что предупреждение выше было не теорией: полка 3
(``_build_layer_3``, счёт через ``ServiceCategory.services``) была пуста
у каждого клиента, а любой явный непустой ``goal`` (ILIKE по
``services__``) отдавал пустую полку 2. Обе поверхности теперь читают
ОБА слоя каталога через ``services.catalog_reads`` — легаси ``Service``
не выключен, к нему добавлен канонический слой. Разрешение категории
там же: своя категория салона побеждает, шаблон — запасной путь.

Приоритет: сказанное сейчас старше выбранного когда-то. Явный ``goal``
в запросе полностью вытесняет сохранённую цель — контракт параметра не
меняется. Полка 1 остаётся goal-независимой по прежней причине: её
якорь — отношения клиента с салоном, а не цель.

Разрешить цель нельзя (нет цели / нет связей / свободный текст без
точного совпадения) → фильтр не применяется, полки прежние. На пилоте
``ClientGoal`` = 0, поэтому это и есть сегодняшнее поведение для всех.
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from goals.wiring import goal_category_ids_for
from services.catalog_reads import (
    catalog_services_for,
    catalog_services_prefetch,
    category_service_counts,
    specialist_service_text_q,
)
from services.models import ServiceCategory
from users.models import SpecialistProfile, TenantUserRelationship
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import success_response


logger = logging.getLogger(__name__)


# Pool caps per layer. Tuned conservatively — Mini App card list
# performance budget is ~10 items per rail before scroll feels slow.
LAYER_1_LIMIT = 5
LAYER_2_LIMIT = 3
LAYER_3_CATEGORY_LIMIT = 10

# Rating threshold for the "Рейтинг X.Y" reasoning fact. Below this we
# omit it to avoid surfacing weak signals as endorsements.
RATING_REASONING_FLOOR = Decimal("4.5")


# ---------------------------------------------------------------------------
# Request / response serializers
# ---------------------------------------------------------------------------


class RecommendationsRequestSerializer(serializers.Serializer):
    lat = serializers.FloatField(
        required=False,
        help_text="Customer's latitude. When provided alongside lon, "
                  "distance feeds the layer_2 ranking and reasoning_text.",
    )
    lon = serializers.FloatField(required=False)
    goal = serializers.CharField(
        required=False, max_length=64, allow_blank=True,
        help_text="Free-text goal like 'маникюр' or 'massage'. "
                  "ILIKE-matched against service.name and "
                  "category.slug. Optional.",
    )


class _SpecialistCardSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    display_name = serializers.CharField()
    avatar_url = serializers.CharField(allow_null=True)
    rating = serializers.DecimalField(
        max_digits=2, decimal_places=1, allow_null=True,
    )
    reviews_count = serializers.IntegerField()
    distance_km = serializers.FloatField(allow_null=True)
    tenant_id = serializers.UUIDField()
    tenant_slug = serializers.CharField()
    tenant_name = serializers.CharField()


class _Layer2ItemSerializer(_SpecialistCardSerializer):
    reasoning_text = serializers.CharField()


class _Layer3CategorySerializer(serializers.Serializer):
    slug = serializers.CharField()
    name = serializers.CharField()
    count = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in km."""
    earth_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    )
    return 2 * earth_km * math.asin(math.sqrt(a))


def _specialist_distance_km(
    specialist: SpecialistProfile,
    *, lat: float | None, lon: float | None,
) -> float | None:
    if (
        lat is None or lon is None
        or specialist.location_lat is None
        or specialist.location_lng is None
    ):
        return None
    return _haversine(
        float(lat), float(lon),
        float(specialist.location_lat), float(specialist.location_lng),
    )


def _build_card(
    specialist: SpecialistProfile,
    *, lat: float | None, lon: float | None,
) -> dict[str, Any]:
    """Common card payload — keeps Layer 1 / 2 / Layer 1-without-reasoning
    rails consistent."""
    return {
        "id": str(specialist.id),
        "display_name": specialist.display_name,
        "avatar_url": specialist.avatar.url if specialist.avatar else None,
        "rating": specialist.rating,
        "reviews_count": specialist.reviews_count,
        "distance_km": _specialist_distance_km(
            specialist, lat=lat, lon=lon,
        ),
        "tenant_id": str(specialist.tenant_id),
        "tenant_slug": specialist.tenant.slug,
        "tenant_name": specialist.tenant.name,
    }


def _compute_layer_2_score(
    specialist: SpecialistProfile,
    *, lat: float | None, lon: float | None,
) -> float:
    """Composite score for Layer 2 ranking. Higher = better.

    Three additive components:
    - Rating: 0..50 (raw rating 0-5 multiplied by 10)
    - Proximity: 100 / (km + 1); when no lat/lon, 0
    - Availability boost: +5 when is_available

    Deliberately simple. Tau §10.3 says priority order is goal >
    distance > availability > rating; we tilt the *score* toward
    distance + rating because goal match is a boolean filter applied
    upstream (in goal-mode the pool is already goal-matching), not a
    score signal here.
    """
    score = 0.0
    if specialist.rating is not None:
        score += float(specialist.rating) * 10.0
    distance = _specialist_distance_km(specialist, lat=lat, lon=lon)
    if distance is not None:
        score += 100.0 / (distance + 1.0)
    if specialist.is_available:
        score += 5.0
    return score


def _goal_matches(specialist: SpecialistProfile, goal: str) -> bool:
    """Cheap goal-match check used by reasoning_text generation.

    The candidate pool is already filtered by goal upstream when
    goal is non-empty; this helper exists so the reasoning_text
    builder can call it without re-filtering, and to handle the
    case where the upstream filter is OR-shape (service name OR
    category slug) — we still want to know if the match was on the
    name (more meaningful) vs slug (more abstract).

    Ходит по ОБОИМ слоям каталога (``services.catalog_reads``). Читая
    только легаси ``services``, эта проверка на пилоте всегда возвращала
    False — и мастер, отобранный ИМЕННО по совпадению с целью, получал
    reasoning_text «Принимает записи» вместо «Совпадает с твоей целью».
    """
    if not goal:
        return False
    needle = goal.lower()
    for service in catalog_services_for(specialist):
        if needle in service.name.lower():
            return True
        if needle in (service.category_slug or "").lower():
            return True
        if needle in (service.category_name or "").lower():
            return True
    return False


def _build_reasoning_text(
    specialist: SpecialistProfile,
    *, lat: float | None, lon: float | None, goal: str,
) -> str:
    """Template-driven reasoning string for Layer 2 items.

    Priority order per Tau §10.3 — emit at most ONE fact for each
    tier in this order, joined by ", ". A combined output reads like:
    "Совпадает с твоей целью, 1.2 км от вас, рейтинг 4.9".

    The empty-fallback case ('Принимает записи') is the truthful
    minimum claim — we never invent availability nor distance when
    the inputs aren't there.
    """
    parts: list[str] = []
    if goal and _goal_matches(specialist, goal):
        parts.append("Совпадает с твоей целью")
    distance = _specialist_distance_km(specialist, lat=lat, lon=lon)
    if distance is not None:
        parts.append(f"{distance:.1f} км от вас")
    if (
        specialist.rating is not None
        and specialist.rating >= RATING_REASONING_FLOOR
    ):
        # Strip trailing zeros so a 4.5 doesn't render as "4.50" —
        # DecimalField(max_digits=2, decimal_places=1) keeps one
        # decimal place internally; normalize() trims the float-style
        # zero for display.
        rating_str = format(specialist.rating, "g")
        parts.append(f"Рейтинг {rating_str}")
    if not parts:
        # Fallback when none of the higher-tier facts apply.
        # is_available is already a pool prerequisite — the claim
        # is truthful: this specialist is open for bookings.
        return "Принимает записи"
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Layer builders
# ---------------------------------------------------------------------------


def _base_pool() -> QuerySet:
    """Goal-independent eligibility pool — Tau §10.2 'simple eligibility'.

    Just "active specialist in an active tenant taking bookings".
    No quality scoring, no geographic radius, no rating threshold.

    Layer 1 ('your salons') uses THIS pool so that typing a goal
    doesn't hide a customer's known salon when that salon doesn't
    happen to offer the goal — identity/relationship anchors layer 1,
    not goal. Layers 2 + 3 apply the goal filter via
    ``_apply_goal_filter``.
    """
    return (
        SpecialistProfile.objects
        .filter(
            is_available=True,
            is_booking_enabled=True,
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )
        # DRF-1430. «in an active tenant» из докстринга выше до сих пор
        # было обещанием, которого код не исполнял: фильтра по салону
        # здесь не было вовсе, а ``select_related("tenant")`` служит
        # только выводу ``tenant_slug``/``tenant_name``. Отключённый
        # салон попадал в «ваши места» наравне с живыми.
        #
        # Здесь INNER JOIN — сознательно, и это ОТЛИЧИЕ от
        # ``RecommendationEngine._fetch_candidates``, где стоит
        # ``Q(tenant__isnull=True) | Q(...)``. Причина в том, что эта
        # поверхность физически не умеет отдать мастера без салона:
        # ``_build_card`` разыменовывает ``specialist.tenant.slug`` и
        # ``.name`` без проверки, так что профиль с ``tenant=NULL``
        # ронял ВЕСЬ эндпоинт в 500 (AttributeError: 'NoneType' object
        # has no attribute 'slug'), а не просто не показывался.
        #
        # То есть фильтр не прячет то, что раньше было видно: он
        # превращает жёсткое падение в корректное отсутствие. Движок
        # подбора тенант не разыменовывает вовсе, поэтому там мастера
        # без салона остаются в выдаче — разная форма условия отражает
        # разные возможности поверхностей, а не разнобой.
        .filter(tenant__is_active=True)
        .select_related("tenant")
        .prefetch_related(*catalog_services_prefetch())
    )


def _apply_goal_filter(qs: QuerySet, goal: str) -> QuerySet:
    """Apply the goal ILIKE filter to a base pool. No-op when goal=''.

    ILIKE on service name OR category slug/name — по ОБОИМ слоям
    каталога (``services.catalog_reads.specialist_service_text_q``).
    Читая только легаси ``services``, этот фильтр на пилоте отдавал
    пустую полку 2 на ЛЮБУЮ непустую строку goal. Post-pilot:
    tag-based or semantic match.
    """
    if not goal:
        return qs
    return qs.filter(specialist_service_text_q(goal)).distinct()


def _apply_goal_category_filter(qs: QuerySet, category_ids) -> QuerySet:
    """Фильтр по разрешённым категориям цели (OD-1). No-op при ``None``.

    Две вещи отличают его от ``_apply_goal_filter`` выше:

    * ни одного ILIKE — категории пришли из курируемой владельцем
      таблицы ``GoalOptionCategory`` через ``goals.resolution``, это
      знание, а не догадка о словах;
    * ходит по КАНОНИЧЕСКОМУ каталогу (``SpecialistService`` ->
      ``SalonService``), а не по легаси ``Service``, который на пилоте
      пуст целиком (замер 2026-08-29: 0 строк против 292 канонических
      связок).

    Предикат общий с движком рекомендаций
    (``RecommendationEngine._goal_category_predicate``), чтобы две
    поверхности не разъехались в трактовке фолбэка на шаблон.
    """
    if not category_ids:
        return qs
    from ai.application.services.recommendation_engine import RecommendationEngine

    return qs.filter(
        RecommendationEngine._goal_category_predicate(tuple(category_ids)),
        specialist_services__is_active=True,
        specialist_services__salon_service__is_active=True,
    ).distinct()


def _build_layer_1(
    base_pool: QuerySet, *, history_tenant_ids: list,
    lat: float | None, lon: float | None,
) -> list[dict]:
    """Layer 1 — customer's known salons, ordered by rating desc.

    Takes the GOAL-INDEPENDENT base pool (see _base_pool docstring).
    Order: rating desc, then id (stable across replicas — Postgres
    otherwise returns LIMIT 5 in arbitrary insertion order).
    """
    if not history_tenant_ids:
        return []
    rows = list(
        base_pool
        .filter(tenant_id__in=history_tenant_ids)
        .order_by("-rating", "id")[:LAYER_1_LIMIT]
    )
    return [_build_card(r, lat=lat, lon=lon) for r in rows]


def _build_layer_2(
    pool: QuerySet, *, history_tenant_ids: list,
    lat: float | None, lon: float | None, goal: str,
) -> list[dict]:
    rows = list(pool.exclude(tenant_id__in=history_tenant_ids))
    # Score + rank. Python-side because the score formula isn't a
    # cheap SQL expression (1 / (distance + 1) for variable distance).
    scored = [
        (r, _compute_layer_2_score(r, lat=lat, lon=lon)) for r in rows
    ]
    # Sort by descending score; ties broken by specialist id for
    # deterministic responses across replicas.
    scored.sort(key=lambda pair: (-pair[1], str(pair[0].id)))
    top_n = scored[:LAYER_2_LIMIT]
    out = []
    for specialist, _score in top_n:
        item = _build_card(specialist, lat=lat, lon=lon)
        item["reasoning_text"] = _build_reasoning_text(
            specialist, lat=lat, lon=lon, goal=goal,
        )
        out.append(item)
    return out


def _build_layer_3(pool: QuerySet) -> dict[str, Any]:
    """Category counts across the eligible pool — feeds the Mini App's
    "browse by category" rail.

    Counts active services within the pool's eligible specialists across
    BOTH catalog layers (``services.catalog_reads``). Раньше считалось
    через ``ServiceCategory.services`` — обратную связь легаси
    ``Service``, — и на пилоте полка была пуста у каждого клиента.

    Категория услуги разрешается с запасным путём через шаблон: своя
    категория салона побеждает, шаблон читается только когда своей нет
    (см. докстринг ``services.catalog_reads``).

    Sorted by count descending, then slug for a stable order across
    replicas. Top-N.
    """
    eligible_ids = list(pool.values_list("id", flat=True))
    counts = category_service_counts(specialist_ids=eligible_ids)
    if not counts:
        return {"categories": []}

    categories = (
        ServiceCategory.objects
        .filter(id__in=counts.keys())
        .values("id", "slug", "name")
    )
    rows = [
        {"slug": row["slug"], "name": row["name"], "count": counts[row["id"]]}
        for row in categories
    ]
    rows.sort(key=lambda row: (-row["count"], row["slug"]))
    return {"categories": rows[:LAYER_3_CATEGORY_LIMIT]}


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class CatalogRecommendationsView(APIView):
    """POST /api/v1/internal/me/catalog/recommendations/"""
    # Bot service auth — same pattern as #97 Records endpoints.
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = RecommendationsRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=RecommendationsRequestSerializer,
        responses={
            200: inline_serializer(
                name="CatalogRecommendationsResponse",
                fields={
                    "data": inline_serializer(
                        name="CatalogRecommendationsData",
                        fields={
                            "layer_1_your_places": _SpecialistCardSerializer(many=True),
                            "layer_2_ayla_picks": _Layer2ItemSerializer(many=True),
                            "layer_3_explore": inline_serializer(
                                name="Layer3Explore",
                                fields={
                                    "categories": _Layer3CategorySerializer(many=True),
                                },
                            ),
                        },
                    ),
                },
            ),
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = RecommendationsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lat = serializer.validated_data.get("lat")
        lon = serializer.validated_data.get("lon")
        goal = (serializer.validated_data.get("goal") or "").strip()

        # Customer's history tenants: TUR rows where the resolved User
        # has an active CUSTOMER role. ADMIN/STAFF rows are
        # operational, not "places you've been booked at".
        history_tenant_ids = list(
            TenantUserRelationship.objects
            .filter(
                user=request.user,
                is_active=True,
                role=TenantUserRelationship.Role.CUSTOMER,
            )
            .values_list("tenant_id", flat=True)
        )

        # One base pool query, two derived views:
        # - layer 1 uses the GOAL-INDEPENDENT base (your salons stay
        #   visible regardless of what the customer is searching for);
        # - layers 2 + 3 apply the goal filter (the picks + category
        #   counts should react to the search).
        # OD-1: сохранённая цель говорит только когда клиент молчит.
        # Явный `goal` в запросе — это сказанное сейчас, и оно старше
        # выбранной когда-то цели: набравший «маникюр» получает
        # маникюр, даже если его цель — «расслабиться». Контракт
        # параметра `goal` при этом не меняется вовсе.
        goal_category_ids = (
            goal_category_ids_for(request.user) if not goal else None
        )

        base_pool = _base_pool()
        scoped_pool = _apply_goal_category_filter(
            _apply_goal_filter(base_pool, goal), goal_category_ids,
        )

        layer_1 = _build_layer_1(
            base_pool, history_tenant_ids=history_tenant_ids,
            lat=lat, lon=lon,
        )
        layer_2 = _build_layer_2(
            scoped_pool, history_tenant_ids=history_tenant_ids,
            lat=lat, lon=lon, goal=goal,
        )
        layer_3 = _build_layer_3(scoped_pool)

        logger.info(
            "catalog.recommendations user_id=%s goal=%r lat=%s lon=%s "
            "l1=%d l2=%d l3_cats=%d",
            request.user.id, goal, lat, lon,
            len(layer_1), len(layer_2),
            len(layer_3.get("categories", [])),
        )

        return success_response({
            "layer_1_your_places": layer_1,
            "layer_2_ayla_picks": layer_2,
            "layer_3_explore": layer_3,
        })
