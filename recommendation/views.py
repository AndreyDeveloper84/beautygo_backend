"""HTTP-проекция границы — `POST /api/v1/internal/recommendation/resolve/`.

Одна из **двух** проекций одной границы (контракт §9.3). Вторая —
внутрипроцессный вызов `recommendation.api.resolve`, которым пользуются
потребители, живущие в этом же процессе. Обе отдают один и тот же
неизменяемый объект; HTTP тут транспорт, а не источник правды.

Представление намеренно тонкое и **ходит через ту же публичную поверхность,
что и все остальные** — `recommendation.api`. Дай мы ему привилегию читать
приватные модули, оно стало бы вторым входом в резолвер, и первый же
«срочный фикс» пошёл бы через него.

Три вещи, которые здесь делаются и которых не было в старой ручке:

1. **Схема на обоих концах.** Вход разбирается схемой, и выход **тоже**
   проверяется схемой перед отправкой. Источник, отдавший форму, которую сам
   не объявлял, обязан узнать об этом первым — а не потребитель, у которого
   она молча не отрисуется.
2. **`resolver_spec_version` в теле.** Потребитель с неизвестной мажорной
   версией обязан вернуть `CONTRACT_VIOLATION`, а не пытаться разобрать.
3. **Ненастроенный источник — это `UNAVAILABLE`, а не пустая выдача.**
   Пустая выдача утверждает «подходящих нет»; мы же не искали.
"""
from __future__ import annotations

import dataclasses
import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response
from users.deletion_requests import deletion_block_for, deletion_refusal

from ._serializers import ResolveRequestSerializer, ResolveResponseSerializer, decision_to_payload
from ._source_binding import CandidateSourceNotConfigured, get_candidate_source
from .api import (
    NeedOrigin,
    NeedSpec,
    Preference,
    PreferenceKind,
    PreferenceOrigin,
    PreferenceStrength,
    RecommendationRequest,
    SafetyState,
    ScopeMode,
    Surface,
    resolve,
)

logger = logging.getLogger(__name__)


def _with_saved_goal(need: NeedSpec, subject) -> NeedSpec:
    """Нужда «по цели» без ключа — ключ сохранённой цели того, кого спрашивают.

    Домашняя полка мини-приложения шлёт ``need.origin=GOAL`` без
    ``goal_key``: бот цели человека не знает, и знать её не должен — она
    живёт здесь. Без подстановки резолвер подбирал так, будто цели нет
    (источник без фильтра цели, S2 «нужда не названа»), хотя путь
    приложения каталога (``users/catalog_recommendations_api``) тому же
    человеку подставлял его цель. Правило то же и читатель тот же —
    :func:`goals.wiring.saved_goal_key_for`, под тем же флагом.

    Названный в теле ключ не перекрывается: сказанное в запросе старше
    сохранённого (OD-1). Другие происхождения нужды не трогаются — у
    «памяти» и «явных слов» своя семантика.
    """
    from goals.wiring import saved_goal_key_for

    if need.origin is not NeedOrigin.GOAL or need.goal_key:
        return need
    saved = saved_goal_key_for(subject)
    if not saved:
        return need
    return dataclasses.replace(need, goal_key=saved)


def _cross_salon_safe(preferences: tuple) -> tuple:
    """Память о мастере или салоне эта ручка не применяет НИКОГДА.

    Любимый мастер — отношение клиента с ОДНИМ салоном (NEVER_CROSSES,
    24.08; узкое дополнение владельца 06.10 — только личная полка «Твои
    места»). Область здесь присылает бот, а ``tenant_refs`` от бота — не
    «свои салоны» клиента: истории салонов он не знает (DRF-1626), а
    ``tenant_refs=[салон B]`` с фаворитом из A и было бы утечкой. Поэтому
    ``confirmed_memory`` вида master/salon отбрасывается при любой
    области, которую назвал бот. Законный путь памяти — режим «свои
    салоны», где салоны выводит сервер (:func:`_within_own_salons`).
    Сказанное в текущем запросе и категория из памяти остаются.
    """
    kept = tuple(
        p for p in preferences
        if not (p.origin is PreferenceOrigin.CONFIRMED_MEMORY
                and p.kind in (PreferenceKind.MASTER, PreferenceKind.SALON))
    )
    if len(kept) != len(preferences):
        logger.warning(
            "recommendation.preference.cross_salon_dropped count=%d — память о мастере/салоне "
            "не ранжирует межсалонную выдачу (NEVER_CROSSES)",
            len(preferences) - len(kept),
        )
    return kept


def _within_own_salons(preferences: tuple, own_salons: tuple) -> tuple:
    """Память о мастере/салоне в режиме «свои салоны» — O-1b (DRF-2831).

    Здесь она законна: область вывел сервер, и в ней только салоны, с
    которыми у клиента есть отношения. Предпочтение, суженное до салона,
    который своим не является (отношения отозваны, id чужой), отбрасывается:
    сузить до него нечего, а расширять до «всех своих» — значит применить
    память там, где её не записывали.
    """
    own = set(own_salons)
    kept = tuple(p for p in preferences if p.tenant_ref is None or p.tenant_ref in own)
    if len(kept) != len(preferences):
        logger.warning(
            "recommendation.preference.foreign_salon_dropped count=%d — салон происхождения "
            "памяти не входит в свои салоны клиента",
            len(preferences) - len(kept),
        )
    return kept


#: Имя не искали: режим не «свои салоны» либо происхождение памяти не позволяет.
_NAME_NOT_APPLICABLE = {"kind": PreferenceKind.MASTER.value, "status": "not_applicable", "matches": 0}


def _resolve_named_masters(named: tuple, own_salons: tuple, viewer) -> tuple[tuple, list[dict]]:
    """Имя мастера из памяти → предпочтение, только среди своих салонов — DRF-2855.

    Ровно одно совпадение — обычное мягкое предпочтение из памяти по этому
    мастеру. Ноль или несколько — не применяется: произвольного мастера не
    выбираем, а ответ несёт признак, чтобы бот уточнил у клиента. Имя, чья
    память записана неизвестно где или в салоне, который клиенту не свой,
    не ищется вовсе (``not_applicable``). На каждое имя — один элемент
    ответа, в порядке запроса. Ни имя, ни id в журнал и в ответ не
    попадают — только исход и число.
    """
    from users.own_salons import masters_matching_name

    own = set(own_salons)
    preferences: list[Preference] = []
    resolution: list[dict] = []
    for item in named:
        if not item.origin_known or (item.tenant_ref is not None and item.tenant_ref not in own):
            logger.warning(
                "recommendation.preference.name_not_applicable — память по имени записана неизвестно "
                "где или в салоне, который клиенту не свой"
            )
            resolution.append(dict(_NAME_NOT_APPLICABLE))
            continue
        salons = own_salons if item.tenant_ref is None else (item.tenant_ref,)
        matches = masters_matching_name(item.stems, salons, viewer=viewer)
        status = "resolved" if len(matches) == 1 else ("not_found" if not matches else "ambiguous")
        logger.info("recommendation.preference.name_resolution status=%s matches=%d", status, len(matches))
        resolution.append({"kind": PreferenceKind.MASTER.value, "status": status, "matches": len(matches)})
        if status == "resolved":
            preferences.append(Preference(
                kind=PreferenceKind.MASTER, ref=matches[0], strength=PreferenceStrength.SOFT,
                origin=PreferenceOrigin.CONFIRMED_MEMORY, tenant_ref=item.tenant_ref,
            ))
    return tuple(preferences), resolution


class RecommendationResolveView(APIView):
    """`POST /api/v1/internal/recommendation/resolve/` — единственная ручка границы."""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = ResolveRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=ResolveRequestSerializer,
        responses={
            200: ResolveResponseSerializer,
            400: OpenApiResponse(description="Запрос не прошёл схему границы"),
            403: OpenApiResponse(description="Bearer / external id неверны"),
            503: OpenApiResponse(description="Источник доменных фактов недоступен — это НЕ пустая выдача"),
        },
    )
    def post(self, request: Request) -> Response:
        # D2 (§7): живая заявка на удаление — рекомендации не считаются.
        # Проверка ДО разбора тела: отказ по воле человека не зависит от
        # формы запроса, и ошибку формы он получать не должен.
        blocked = deletion_block_for(request.user)
        if blocked is not None:
            return deletion_refusal(blocked)
        serializer = ResolveRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # DRF-2420: границы показа демо — по личности, за которую
            # спрашивает бот, а не по самому боту.
            source = get_candidate_source(viewer=request.user)
        except CandidateSourceNotConfigured as exc:
            # Отдельная ветка и отдельный код: недоступность источника не
            # должна выглядеть как «посмотрели и не нашли». Именно слияние
            # этих двух состояний в одну ветку и стоило нам DEFECT-C-02.
            logger.error("recommendation.resolve.source_unavailable: %s", exc)
            return error_response(
                ErrorCode.SERVICE_UNAVAILABLE,
                "candidate source is not bound — this is unavailability, not an empty result",
                status_code=503,
            )

        scope = serializer.build_scope()
        preferences = serializer.build_preferences()
        named = serializer.named_masters()
        preference_resolution: list[dict] = []
        if scope.mode is ScopeMode.OWN_SALONS:
            # Салоны клиента называет сервер — по тому же правилу, что полка
            # «Твои места». Бот их не присылает и прислать не может (схема).
            from users.own_salons import own_salon_ids

            scope = dataclasses.replace(scope, tenant_refs=own_salon_ids(request.user))
            preferences = _within_own_salons(preferences, scope.tenant_refs)
            resolved, preference_resolution = _resolve_named_masters(named, scope.tenant_refs, request.user)
            preferences += resolved
        else:
            preferences = _cross_salon_safe(preferences)
            if named:
                # Имя по всему каталогу не ищется: это и была бы межсалонная
                # передача (NEVER_CROSSES). Само имя в лог не пишется.
                logger.warning(
                    "recommendation.preference.name_dropped count=%d — имя мастера разрешается "
                    "только в режиме «свои салоны»",
                    len(named),
                )
                preference_resolution = [dict(_NAME_NOT_APPLICABLE) for _ in named]

        decision = resolve(
            RecommendationRequest(
                request_id=serializer.validated_data["request_id"],
                # Кого спрашивают — говорит аутентификация, а не тело запроса.
                subject_ref=str(request.user.id),
                surface=Surface(serializer.validated_data["surface"]),
                scope=scope,
                need=_with_saved_goal(serializer.build_need(), request.user),
                constraints=serializer.build_constraints(),
                safety_state=SafetyState(serializer.validated_data["safety_state"]),
                tie_break_seed=serializer.validated_data.get("tie_break_seed"),
                k=serializer.validated_data["k"],
                preferences=preferences,
            ),
            source=source,
        )

        payload = decision_to_payload(decision, preference_resolution=preference_resolution)
        # Проверка СВОЕГО выхода. Не паранойя: у формы ответа теперь есть
        # владелец (§2.1 C3), и владелец обязан ловить своё нарушение сам —
        # иначе он снова окажется у потребителя в виде пустого экрана.
        outgoing = ResolveResponseSerializer(data=payload)
        outgoing.is_valid(raise_exception=True)

        logger.info(
            "recommendation.resolve request_id=%s surface=%s scope=%s ordered=%d excluded=%d tiers=%d",
            decision.request_id,
            serializer.validated_data["surface"],
            scope.mode.value,
            len(decision.ordered),
            len(decision.excluded),
            len({c.tier for c in decision.ordered}),
        )
        return success_response(payload)
