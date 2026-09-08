"""POST /api/v1/internal/me/catalog/recommendations/ — поверхность домашнего экрана.

**Эта поверхность больше НЕ ранжирует.** Порядок кандидатов даёт
Recommendation Resolver — единственный владелец `RecommendationDecision`
(решение владельца `docs/OPEN_DECISIONS.md` §53, контракт
`RECOMMENDATION_RESOLVER_CONTRACT_v1.0.md`). Здесь остались только
границы поиска, проекция решения в полки и форма карточки.

Что упразднено этой правкой (контракт §16)
------------------------------------------
* ``_compute_layer_2_score`` — `rating*10 + 100/(km+1) + 5·available`.
  Расстояние не срабатывало никогда (фронт шлёт пустое тело), `+5`
  получали все (`is_available` — предусловие пула), значит это была
  **сортировка по рейтингу и ничем больше**. Рейтинг — вторичное
  свидетельство после соответствия нужде (канон §9.1), а на пилоте
  он вообще не участвует в сортировке (решение владельца §29.4).
* ``_build_reasoning_text`` — строка для показа, которую собирал
  источник. Теперь наружу идут `reason_codes` + `evidence`, а фразу
  собирает представление (§7). Потребитель WHY не придумывает.
* ``RATING_REASONING_FLOOR = 4.5`` — порог, защищавший от НИЗКОЙ оценки
  и не защищавший от ОТСУТСТВУЮЩЕЙ. Отсюда «Рейтинг 4.9» при нуле
  отзывов: литерал из сида, поставленный ради порога чужого движка,
  вышел человеку как причина выбора мастера. Заменён силой свидетельства
  (§8.3): при `review_count = 0` рейтинг — `UNSUBSTANTIATED` и в WHY
  не попадает, но **передаётся** как справочное число.
* ``order_by("-rating", "id")[:5]`` в полке 1 — лексикографика под
  отсечением.

Что осталось и почему это не ранжирование
-----------------------------------------
* **Полка 1 «твои салоны»** — не порядок, а **scope**: отдельный вызов
  резолвера с `tenant_refs` = салоны, где у человека активная связь.
  Якорь полки — отношения, а не качество.
* **Полка 2** — тот же вызов с `exclude_tenant_refs` = те же салоны.
* **Полка 3** — счётчики категорий. Это **агрегат каталога, а не
  рекомендация**: `catalog_visible ≠ recommendation_eligible` (§10.1).
  Никаких кандидатов она не упорядочивает и не отсекает по качеству.

Почему сегодня полки 1 и 2 пусты — и это честно
-----------------------------------------------
`recommendation_eligible = (mapping_status == VERIFIED)`, а шкалы доверия
к маппингу в схеме **нет** (замер 07.09: 206 из 265 услуг связаны
с шаблоном, признака доверия не существует как поля). Значит S1 не
пропускает никого, `ordered` пуст, а в `excluded` стоит
`ELIG_EXCLUDED_NOT_RECOMMENDABLE` — сигнал §10.3
`BLOCKED(CATALOG_NOT_RECOMMENDABLE)`.

Это **состояние**, а не поломка, и снимается оно одним решением
владельца (OD §40.4 п.1) через политику `mapping_override_enabled`
(§10.4). Пустой честный ответ допустимее непустого недоказанного —
и именно поэтому «Рейтинг 4.9» здесь больше не появится.

Состояние безопасности
----------------------
Резолвер требует `safety_state`. У этой поверхности своего источника
безопасности нет: разговора здесь не ведётся, Safety Engine ничего про
этот запрос не говорил. Поэтому состояние приходит **от вызывающего**,
а его отсутствие — `UNKNOWN`, то есть fail-closed по §14. Это логируется
отдельно (`catalog.recommendations.safety_state_missing`): «мы не знаем,
безопасно ли» не должно молча выглядеть как «подходящих нет».
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from goals.wiring import goal_category_ids_for
from recommendation.api import (
    NeedOrigin,
    StagePolicy,
    NeedSpec,
    RecommendationDecision,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
    resolve,
)
from services.catalog_reads import category_service_counts
from services.models import ServiceCategory
from users.models import SpecialistProfile, TenantUserRelationship
from users.permissions import IsBotServiceWithVerifiedClient
from users.recommendation_source import SpecialistCandidateSource
from users.response import success_response


logger = logging.getLogger(__name__)


# Сколько ПОКАЗЫВАЕТСЯ на полке. Не сколько считать: резолвер отдаёт всё
# допустимое множество, срез делает поверхность — и делает его ПОСЛЕ
# ротации, иначе «Показать ещё» нечего показывать (§12.2).
LAYER_1_LIMIT = 5
LAYER_2_LIMIT = 3
LAYER_3_CATEGORY_LIMIT = 10


# ---------------------------------------------------------------------------
# Request / response serializers
# ---------------------------------------------------------------------------


class RecommendationsRequestSerializer(serializers.Serializer):
    lat = serializers.FloatField(required=False)
    lon = serializers.FloatField(required=False)
    goal = serializers.CharField(
        required=False, max_length=64, allow_blank=True,
        help_text="Что человек ищет сейчас. Уходит в NeedSpec.raw_text.",
    )
    safety_state = serializers.ChoiceField(
        choices=[s.value for s in SafetyState], required=False,
        help_text=(
            "Состояние безопасности от того, кто его знает. Отсутствие — "
            "UNKNOWN, то есть fail-closed по §14: выдача пуста."
        ),
    )


class _SpecialistCardSerializer(serializers.Serializer):
    """Карточка. Числа здесь — справочные, причиной является только код."""

    id = serializers.UUIDField()
    display_name = serializers.CharField()
    avatar_url = serializers.CharField(allow_null=True)
    rating = serializers.DecimalField(max_digits=2, decimal_places=1, allow_null=True)
    reviews_count = serializers.IntegerField()
    tenant_id = serializers.UUIDField()
    tenant_slug = serializers.CharField()
    tenant_name = serializers.CharField()
    tier = serializers.IntegerField()
    reason_codes = serializers.ListField(child=serializers.CharField())
    evidence = serializers.ListField(child=serializers.DictField())


class _Layer3CategorySerializer(serializers.Serializer):
    slug = serializers.CharField()
    name = serializers.CharField()
    count = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Проекция решения в полку
# ---------------------------------------------------------------------------


def _resolve_layer(
    *,
    request_id: str,
    subject_ref: str,
    scope: Scope,
    need: NeedSpec,
    safety_state: SafetyState,
    seed: str | None,
    k: int,
) -> RecommendationDecision:
    """Один вызов границы. Порядок — его, границы — наши."""
    return resolve(
        RecommendationRequest(
            request_id=request_id,
            subject_ref=subject_ref,
            surface=Surface.MINIAPP_HOME,
            scope=scope,
            need=need,
            safety_state=safety_state,
            tie_break_seed=seed,
            k=k,
        ),
        source=SpecialistCandidateSource(),
        policy=_stage_policy(),
    )


def _stage_policy() -> StagePolicy:
    """Политика стадий из настроек. Единственная ручка — исключение §10.4.

    ``RECOMMENDATION_PILOT_MAPPING_OVERRIDE`` реализует ответ владельца
    (а) «на пилоте считать каталог VERIFIED». По умолчанию ВЫКЛЮЧЕНО:
    ответа нет, а включить его самим значило бы ответить за владельца.

    Когда включено, каждый затронутый кандидат получает свидетельство
    ``strength=UNSUBSTANTIATED, source_ref="pilot_override"`` — разрешённое
    исключение обязано быть видно в свидетельстве, а не растворяться
    в умолчании. Иначе через месяц никто не отличит «проверено» от
    «разрешено на время пилота».
    """
    return StagePolicy(
        mapping_override_enabled=bool(
            getattr(settings, "RECOMMENDATION_PILOT_MAPPING_OVERRIDE", False)
        ),
    )


def _project(decision: RecommendationDecision, *, limit: int) -> list[dict[str, Any]]:
    """Разложить решение в карточки. Срез — ПОСЛЕ ротации, не в SQL.

    Порядок карточек — порядок решения. Поверхность его не меняет: ей
    разрешено показать первые k, но не переставить (§2.1 C1).
    """
    shown = decision.ordered[:limit]
    if not shown:
        return []

    profiles = {
        profile.id: profile
        for profile in (
            SpecialistProfile.objects
            .filter(id__in=[c.candidate_ref.id for c in shown])
            .select_related("tenant")
        )
    }
    cards = []
    for candidate in shown:
        profile = profiles.get(candidate.candidate_ref.id)
        if profile is None:
            # Кандидат исчез между решением и отрисовкой: домен изменился.
            # Пропускаем молча — но НЕ подставляем другого: подмена
            # запрещена (§14).
            continue
        cards.append({
            "id": str(profile.id),
            "display_name": profile.display_name,
            "avatar_url": profile.avatar.url if profile.avatar else None,
            "rating": profile.rating,
            "reviews_count": profile.reviews_count,
            "tenant_id": str(profile.tenant_id),
            "tenant_slug": profile.tenant.slug,
            "tenant_name": profile.tenant.name,
            "tier": candidate.tier,
            "reason_codes": [code.value for code in candidate.reason_codes],
            "evidence": [_evidence_payload(item) for item in candidate.evidence],
        })
    return cards


def _evidence_payload(item) -> dict[str, Any]:
    """Свидетельство наружу: значение ВМЕСТЕ с силой.

    Сила — то, чего не было в старой форме. Без неё потребитель получал
    голое число и решал сам, причина это или украшение; решил неверно,
    и человек прочитал «Рейтинг 4.9» при нуле отзывов.
    """
    value = item.value
    if hasattr(value, "rating") and hasattr(value, "review_count"):
        value = {"rating": str(value.rating), "review_count": value.review_count}
    elif hasattr(value, "value"):
        value = value.value
    return {
        "kind": item.kind.value,
        "strength": item.strength.value,
        "origin": item.origin.value,
        "value": value,
        "source_ref": item.source_ref,
    }


def _build_layer_3(specialist_ids: list) -> dict[str, Any]:
    """Счётчики категорий по видимому каталогу. НЕ рекомендация.

    `catalog_visible ≠ recommendation_eligible` (§10.1): полка отвечает
    на вопрос «что вообще есть», а не «что тебе подходит». Поэтому она
    считается по пулу каталога и живёт даже тогда, когда рекомендовать
    нельзя никого.
    """
    counts = category_service_counts(specialist_ids=specialist_ids)
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


def _catalog_pool() -> QuerySet:
    """Видимый каталог — для полки 3. Допустимость домена, не политика."""
    return SpecialistProfile.objects.filter(
        is_available=True,
        is_booking_enabled=True,
        status=SpecialistProfile.ProfileStatus.ACTIVE,
        tenant__is_active=True,
    )


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class CatalogRecommendationsView(APIView):
    """POST /api/v1/internal/me/catalog/recommendations/"""

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
                            "layer_2_ayla_picks": _SpecialistCardSerializer(many=True),
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
        goal = (serializer.validated_data.get("goal") or "").strip()

        raw_safety = serializer.validated_data.get("safety_state")
        if raw_safety is None:
            # Отсутствие состояния безопасности — не «всё в порядке».
            # Громко, потому что молчание здесь неотличимо от пустой
            # выдачи, а цена этих двух состояний противоположна.
            logger.warning(
                "catalog.recommendations.safety_state_missing user_id=%s — "
                "fail-closed по §14: выдача будет пуста",
                request.user.id,
            )
        safety_state = SafetyState(raw_safety) if raw_safety else SafetyState.UNKNOWN

        history_tenant_ids = list(
            TenantUserRelationship.objects
            .filter(
                user=request.user,
                is_active=True,
                role=TenantUserRelationship.Role.CUSTOMER,
            )
            .values_list("tenant_id", flat=True)
        )

        # Курируемая цель говорит, только когда человек молчит: сказанное
        # сейчас старше выбранного когда-то (OD-1). Ключ цели уходит
        # в нужду, а связку «цель → категории» разворачивает домен —
        # там же, где она курируется.
        goal_key = None if goal else _saved_goal_key(request.user)
        need = NeedSpec(
            origin=NeedOrigin.USER_EXPLICIT if goal else NeedOrigin.GOAL,
            goal_key=goal_key,
            raw_text=goal or None,
        )
        seed = f"miniapp-home:{request.user.id}"
        request_id = str(uuid.uuid4())

        # Полка 1 — БЕЗ нужды, и это прежнее поведение, а не упрощение.
        #
        # Её якорь — отношения человека с салоном, а не то, что он ищет
        # сейчас: набравший «массаж» не должен терять свой маникюрный
        # салон из «твоих мест». Названная нужда — условие допустимости
        # (S1), поэтому передать её сюда значило бы отфильтровать полку 1
        # по цели, чего она никогда не делала.
        layer_1_decision = _resolve_layer(
            request_id=request_id,
            subject_ref=str(request.user.id),
            scope=Scope(ScopeMode.MARKETPLACE, tenant_refs=tuple(history_tenant_ids)),
            need=NeedSpec(origin=NeedOrigin.MEMORY),
            safety_state=safety_state,
            seed=seed,
            k=LAYER_1_LIMIT,
        ) if history_tenant_ids else None

        layer_2_decision = _resolve_layer(
            request_id=request_id,
            subject_ref=str(request.user.id),
            scope=Scope(
                ScopeMode.MARKETPLACE,
                exclude_tenant_refs=tuple(history_tenant_ids),
            ),
            need=need,
            safety_state=safety_state,
            seed=seed,
            k=LAYER_2_LIMIT,
        )

        layer_1 = _project(layer_1_decision, limit=LAYER_1_LIMIT) if layer_1_decision else []
        layer_2 = _project(layer_2_decision, limit=LAYER_2_LIMIT)
        layer_3 = _build_layer_3(list(_catalog_pool().values_list("id", flat=True)))

        logger.info(
            "catalog.recommendations user_id=%s goal=%r goal_key=%r safety=%s "
            "l1=%d l2=%d l3_cats=%d excluded=%d decision_codes=%s",
            request.user.id, goal, goal_key, safety_state.value,
            len(layer_1), len(layer_2), len(layer_3.get("categories", [])),
            len(layer_2_decision.excluded),
            [code.value for code in layer_2_decision.reason_codes],
        )

        return success_response({
            "layer_1_your_places": layer_1,
            "layer_2_ayla_picks": layer_2,
            "layer_3_explore": layer_3,
        })


def _saved_goal_key(client) -> str | None:
    """Ключ сохранённой цели человека, если фильтр цели включён.

    Через ``goals.wiring``: флаг ``GOAL_RESOLUTION_ENABLED`` читается
    ровно в одном месте репозитория, и это место — не здесь.
    """
    from goals.models import ClientGoal

    if goal_category_ids_for(client) is None:
        return None
    goal = (
        ClientGoal.objects.filter(client=client, is_active=True)
        .order_by("-selected_at")
        .first()
    )
    return goal.goal_key if goal and goal.goal_key else None
