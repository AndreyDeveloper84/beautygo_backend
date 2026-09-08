"""POST /api/v1/internal/me/catalog/recommendations/ — поверхность домашнего экрана.

**Эта поверхность больше НЕ ранжирует.** Порядок кандидатов даёт
Recommendation Resolver — единственный владелец `RecommendationDecision`
(решение владельца `docs/OPEN_DECISIONS.md` §53, контракт
`RECOMMENDATION_RESOLVER_CONTRACT_v1.0.md`). Здесь остались только
границы поиска и проекция решения в полки.

Что полка отдаёт — и чего не отдаёт
-----------------------------------
Строка полки несёт **ссылку на кандидата** (`candidate: {kind, id}`),
его место (`rank`, `tier`) и причину (`reason_codes`, `evidence`).
Имени, фото и рейтинга в ней нет: показ берётся из зеркала бота, где
лежат и собственные ключи потребителя, и данные соседних блоков экрана.

Разделение простое: **личность, порядок и причина — наши; показ —
зеркала**. Перевод нашего ключа в ключ зеркала делает транзит
(`CatalogMaster.ayla_user_id`); у клиента такого поля нет вовсе.

Вид кандидата объявляется явно, а не подразумевается (контракт §5, K1).
Резолвер производит `PROVIDER` — мастеров, не услуги, — и всё, чем он
различает, висит на специалисте: рейтинг и отзывы (S5), прошлые визиты
(S4), расписание (S3). Пока вид подразумевался, существовал ответ,
который одна половина границы считала валидным, а другая молча
отбрасывала целиком — и заметно это стало бы не сразу, а в день, когда
кто-то разметит связи и станет ждать, что полка загорится.

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
`recommendation_eligible = (mapping_status == VERIFIED)`. Шкала теперь
есть (§76, `SalonService.mapping_status`), но **подтверждённых связей
ноль**: 206 связей пилота поставлены демо-сидом и после миграции стоят
`REVIEW_REQUIRED`, а у единственного салона с настоящей интеграцией
шаблонов нет ни у одной из 58 услуг (замер 08.09, срок годности есть).

Значит S1 не пропускает никого, `ordered` пуст, а в `excluded` стоит
`ELIG_EXCLUDED_NOT_RECOMMENDABLE` — сигнал §10.3
`BLOCKED(CATALOG_NOT_RECOMMENDABLE)`, он же `NO_VERIFIED_CANDIDATES`
из §76.

Это **штатное состояние с именем**, а не поломка, и снимается оно
**подтверждением связей**, а не настройкой: путь, которым непроверенная
связь попадала в подбор, удалён вместе с возможностью (T16). Пустой
честный ответ допустимее непустого недоказанного — и именно поэтому
«Рейтинг 4.9» здесь больше не появится.

Состояние безопасности — `NOT_APPLICABLE` по ТИПУ поверхности
--------------------------------------------------------------
Решение владельца (`OPEN_DECISIONS.md` §72, вопрос §71). Здесь не ведётся
разговор, не выполняется интерпретация запроса и не формируется
health-sensitive рекомендация — значит оценка безопасности не «неизвестна»,
а **неприменима**, и гейт не применяется. `UNKNOWN` означало бы «оценка
применима, данных нет» и гасило бы полку, ничего при этом не предотвращая:
те же мастера видны и бронируемы в обычном каталоге одним тапом.

**Поле безопасности в запросе отсутствует, и это конструкция, а не
упущение.** Владелец назвал поимённо запрещённый вид::

    safety = payload.get("safety") or NOT_APPLICABLE   # fail-open дыра

Разница между ним и правильным кодом на глаз неразличима: оба дают
`NOT_APPLICABLE`, когда поля нет. Поэтому поля здесь нет **вовсе** —
состояние определяется типом поверхности до выполнения решения, и путь,
которым отсутствие данных превратилось бы в неприменимость, не закрыт
дисциплиной, а **не существует**. Вызывающий, у которого есть настоящий
`SafetyResult`, по определению другая поверхность: ему `resolve()`
и `Surface.BOT_CHAT`, а не эта витрина.

Заявление проверяется **содержанием решения**, а не доверием к этой
ручке: кандидат с `requires_health_check` или активная персонализация S4
отменяют `NOT_APPLICABLE`, и решение становится fail-closed, как при
`UNKNOWN` (`recommendation/_stages.py`, контракт §4.1). Витрина, в которой
есть услуга с противопоказаниями, витриной уже не является.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from goals.wiring import goal_category_ids_for
from recommendation.api import (
    NeedOrigin,
    NeedSpec,
    RecommendationDecision,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
    resolve,
)
from services.catalog_reads import category_service_counts, specialist_service_text_q
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
    # Поля безопасности здесь НЕТ намеренно — см. модульный докстринг.
    # Принять его значило бы завести ровно ту конструкцию, которую владелец
    # запретил поимённо (§72): «поля нет → NOT_APPLICABLE» неотличимо от
    # «данных нет → NOT_APPLICABLE», а второе — fail-open дыра.


class _CandidateRefSerializer(serializers.Serializer):
    """Ссылка на кандидата: ВИД и ключ, всегда вместе (контракт §5, K1).

    Вид объявляется явно, а не подразумевается. Пока он подразумевался,
    существовал ответ, который одна половина границы считала валидным,
    а другая молча отбрасывала целиком.
    """

    kind = serializers.CharField()
    id = serializers.UUIDField()


class _ShelfRowSerializer(serializers.Serializer):
    """Строка полки — проекция решения, а не карточка.

    Имени, фото и рейтинга здесь нет намеренно: показ берётся из
    зеркала, где лежат и ключи потребителя. Наше — личность, порядок
    и причина; прислав ещё и показ, мы завели бы второй источник тех же
    полей.
    """

    candidate = _CandidateRefSerializer()
    rank = serializers.IntegerField()
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
        # Политику НЕ собираем: её читает резолвер сам
        # (`StagePolicy.from_settings`). Здесь стояла своя сборка из
        # настроек — и она давала одной политике два значения в одном
        # процессе, потому что HTTP-проекция звала `resolve()` без неё
        # и получала жёсткое умолчание. Поверхность не владеет политикой
        # ровно по той же причине, по которой не владеет порядком.
    )


def _project(decision: RecommendationDecision, *, limit: int) -> list[dict[str, Any]]:
    """Разложить решение в строки полки. Срез — ПОСЛЕ ротации, не в SQL.

    Порядок строк — порядок решения. Поверхность его не меняет: ей
    разрешено показать первые k, но не переставить (§2.1 C1).

    Строка несёт ССЫЛКУ на кандидата, а не карточку
    ----------------------------------------------
    Здесь собиралась карточка: имя, аватар, рейтинг, салон. Больше нет,
    и причины две, обе про один и тот же класс дефекта.

    **Первая — вид кандидата был неявным.** Строка отдавала поле `id`
    и молчала о том, чей это ключ. Резолвер производит `PROVIDER`
    (контракт §5, K1 требует объявлять вид явно), а потребитель на другой
    стороне границы отбирал кандидатов вида `SERVICE` и склеивал их со
    своим списком услуг. Совпасть это не могло **никогда**, но заметно
    стало бы не сразу: пока подтверждённых связей ноль, выдача и так
    пуста. Проявилось бы это в день, когда кто-то разметит связи и станет
    ждать, что полка загорится, — с готовым ложным объяснением
    «наверное, опять разметка».

    Теперь вид едет в ответе: `candidate: {kind, id}`. Несовпадение
    предметов становится видно на первом же ответе, а не через месяц.

    **Вторая — данные для показа у нас и у потребителя разные.** Экран
    рисует мастеров из зеркала бота, там же лежат его собственные ключи
    и там же он берёт имя и фото для соседних блоков. Прислав своё имя
    и свой рейтинг, мы завели бы второй источник тех же полей — ровно
    то расхождение, которое эпик и убирает, только в отображении.

    Отсюда разделение: **личность, порядок и причина — наши; показ —
    зеркала**. Перевод нашего ключа в ключ зеркала делает транзит,
    у которого есть `CatalogMaster.ayla_user_id`; у клиента такого поля
    нет вовсе.

    Ушла и молчаливая ветка «профиль не найден — пропускаем». Она
    выбрасывала кандидата из выдачи без следа, то есть решала за
    человека, кого он увидит, — отбор, то есть политика, то есть
    четвёртый авторитет, живущий в проекции. Кандидат, которого нельзя
    отрисовать, обязан становиться названным состоянием на стороне,
    которая про отрисовку знает, а не исчезать здесь.

    Побочно исчез и запрос к базе: проекции больше нечего дочитывать.
    """
    return [
        {
            "candidate": {
                "kind": candidate.candidate_ref.kind.value,
                "id": str(candidate.candidate_ref.id),
            },
            "rank": candidate.rank,
            "tier": candidate.tier,
            "reason_codes": [code.value for code in candidate.reason_codes],
            "evidence": [_evidence_payload(item) for item in candidate.evidence],
        }
        for candidate in decision.ordered[:limit]
    ]


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


def _catalog_pool(*, goal: str, goal_category_ids) -> QuerySet:
    """Видимый каталог для полки 3, суженный тем, что человек ищет.

    Сужение целью здесь — **чтение каталога, а не политика рекомендации**:
    полка отвечает «что вообще есть по этому запросу», и реагировать на
    запрос она обязана. Я её однажды уже расширил до всего каталога молча,
    и прогон это поймал: счётчики категорий перестали отзываться на поиск,
    хотя ровно за этим их и показывают.

    Ранжирования здесь нет и быть не может: это счётчики, а не кандидаты.
    """
    pool = SpecialistProfile.objects.filter(
        is_available=True,
        is_booking_enabled=True,
        status=SpecialistProfile.ProfileStatus.ACTIVE,
        tenant__is_active=True,
    )
    if goal:
        pool = pool.filter(specialist_service_text_q(goal)).distinct()
    elif goal_category_ids:
        from ai.application.services.recommendation_engine import RecommendationEngine

        pool = pool.filter(
            RecommendationEngine._goal_category_predicate(tuple(goal_category_ids)),
            specialist_services__is_active=True,
            specialist_services__salon_service__is_active=True,
        ).distinct()
    return pool


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
                            "layer_1_your_places": _ShelfRowSerializer(many=True),
                            "layer_2_ayla_picks": _ShelfRowSerializer(many=True),
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

        # Константа поверхности, не производная от запроса (§72).
        # Ни `get`, ни `or`, ни умолчания сериализатора: значение известно
        # ДО того, как появился запрос, потому что определяется тем, чем
        # эта ручка является, а не тем, что ей прислали.
        safety_state = SafetyState.NOT_APPLICABLE

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
        goal_category_ids = None if goal else goal_category_ids_for(request.user)
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
        layer_3 = _build_layer_3(list(
            _catalog_pool(goal=goal, goal_category_ids=goal_category_ids)
            .values_list("id", flat=True)
        ))

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
