"""Стадии S0–S5 — контракт §3. Каждая отвечает за своё и не знает про сумму.

Ключевое устройство: стадия **не выдаёт балл**. Она выдаёт ключ сравнения,
и конвейер (`_pipeline`) применяет ключи **лексикографически**: сначала S2,
и только внутри неразличённых — S3, и так далее. Единая взвешенная сумма
запрещена буквой канона §9 («Ayla does not use one global ranking formula»)
и одновременно является сегодняшним дефектом движка, где соответствие нужде
весит 0.15, а рейтинг 0.30 — вдвое больше.

Три решения, принятые при реализации, а не унаследованные из текста:

**1. Стадия, неактивная хотя бы для одного кандидата группы, не различает
никого в этой группе.** Иначе «не понижать» (§29.4) неисполнимо: если
кандидату без отзывов дать ключ 0, а соседу с подтверждённым рейтингом —
единицу, второй уедет в ярус выше, то есть первый будет понижен за
отсутствие данных. Держать их «равными каждому по отдельности» нельзя —
порядок перестал бы быть транзитивным. Остаётся единственный непротиворечивый
вариант: стадия молчит про всю группу. Проверяется тестом E4.

**2. `WEAK` не упорядочивает ничего.** Контракт §8.3 допускает `WEAK` как
тай-брейк внутри яруса, но решение владельца §29.4 говорит жёстче:
«рейтинг в сортировку пока не включать». Приоритет у владельца, и это как
раз тот случай, где его решение ужесточает канон. Практически: пока
`N_substantiated` не назначен, `CONFIRMED` не выдаётся вовсе, то есть S5
сегодня молчит для всех — и это правильное состояние пилота, а не заглушка.

**3. Бюджет, который нечем подтвердить, — не пройден.** Явный предел цены
это жёсткое ограничение S1 (§3.4). Кандидат с неизвестной ценой не может
быть подтверждён как удовлетворяющий ему, а S1 fail-closed. Если владелец
захочет «показывать и с неизвестной ценой» — это продуктовое решение,
и приниматься оно должно им, а не здесь.
"""
from __future__ import annotations

import logging

from dataclasses import dataclass, field
from typing import Mapping, Sequence
from uuid import UUID

from ._evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceStrength,
    rating_evidence,
    rating_strength,
)
from ._reason_codes import ReasonCode
from ._types import (
    MATCH_CODE,
    MATCH_RANK,
    CandidateFacts,
    CandidateKind,
    ConstraintKind,
    ExcludedCandidate,
    MappingCensus,
    MappingStatus,
    MatchLevel,
    NeedSpec,
    Preference,
    PreferenceKind,
    PreferenceOrigin,
    PreferenceStrength,
    RecommendationRequest,
    SafetyState,
    ScheduleState,
    Scope,
    StageId,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StagePolicy:
    """Настраиваемые пороги. Все — `CONTROLLED_POLICY`, все с версией.

    Умолчания описывают **сегодняшний контур**, а не желаемое состояние:
    маппинга нет, расписание подтверждено у четырёх мастеров из тридцати
    одного, порога подтверждённости отзывов владелец не назвал. Умолчание,
    описывающее желаемое, молча включило бы стадию, под которой нет данных.
    """

    version: str = "1.0.0"

    # Здесь стоял `mapping_override_enabled` — §10.4 вариант (а), «на
    # пилоте считать каталог VERIFIED». Владелец ответил иначе (§76):
    # «ноль VERIFIED не разрешает fallback на REVIEW_REQUIRED, иначе
    # статус будет декоративным».
    #
    # Значит это не выключенная возможность, а запрещённая, и оставить
    # её в положении «выкл» значило бы оставить путь, которым запрет
    # обходится одной строкой в settings. Убран путь, а не соблазн —
    # тем же приёмом, которым из схемы запроса убрано поле безопасности
    # (§72) и по тому же порядку предпочтения (§74).

    #: K6: признак доступности включается в S3 только когда подтверждённое
    #: расписание есть минимум у 80% активных бронируемых мастеров.
    #: Флаг говорит «условие выполнено», значение порога — решение владельца.
    availability_ranking_enabled: bool = False

    #: §8.3. `None` = порог не назначен, `CONFIRMED` не выдаётся.
    n_substantiated: int | None = None

    @classmethod
    def from_settings(cls) -> "StagePolicy":
        """Политика читает свои настройки САМА — не поверхность за неё.

        Метод остаётся, хотя читать ему сейчас нечего: единственная
        настройка, которая здесь была, — пилотное исключение маппинга —
        удалена вместе с самой возможностью (§76). Оставлен он не «на
        будущее», а потому что снятие его вернуло бы поверхностям право
        собирать политику самим, а именно оттуда взялся разрыв, который
        мы только что закрыли: домашний экран читал настройку у себя,
        HTTP-проекция получала жёсткое умолчание, и одна политика имела
        два значения в одном процессе.

        Пороги `availability_ranking_enabled` и `n_substantiated` —
        `CONTROLLED_POLICY`, оба висят у владельца. Когда он их назовёт,
        читать их будет этот метод, и ровно одно место.
        """
        return cls()


@dataclass(frozen=True)
class StageOutput:
    """Результат одной ранжирующей стадии (S2–S5).

    `keys` — сравнимые величины, больше значит лучше. Наружу они не уходят
    никогда: `RecommendationDecision` несёт ярусы и коды, но не числа
    (канон §8).
    """

    stage: StageId
    active: bool
    inactive_reason: str | None = None
    keys: Mapping[UUID, float] = field(default_factory=dict)
    inactive_for: frozenset[UUID] = frozenset()
    codes: Mapping[UUID, frozenset[ReasonCode]] = field(default_factory=dict)
    evidence: Mapping[UUID, tuple[EvidenceItem, ...]] = field(default_factory=dict)
    #: Кандидаты, которым запрещён первый ярус (K5).
    tier_one_forbidden: frozenset[UUID] = frozenset()
    #: Стадия из нескольких измерений (S4, O-1): ключ — кортеж, сравнение
    #: по измерениям по очереди. Если задан, группа делится по нему, а не по
    #: ``keys``.
    lex_keys: Mapping[UUID, tuple[int, ...]] = field(default_factory=dict)
    #: С какого измерения значение кандидата неизвестно (§29.4 по измерению):
    #: подгруппа, где он стоит, дальше этого измерения не делится. Старшие
    #: измерения работают; неизвестное молчит вместе со всеми младшими.
    unknown_from: Mapping[UUID, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionResult:
    """Что осталось после S0–S1 и почему остальные выбыли."""

    admitted: tuple[CandidateFacts, ...]
    excluded: tuple[ExcludedCandidate, ...]
    codes: Mapping[UUID, frozenset[ReasonCode]] = field(default_factory=dict)
    evidence: Mapping[UUID, tuple[EvidenceItem, ...]] = field(default_factory=dict)
    census: MappingCensus = field(default_factory=MappingCensus)


# ---------------------------------------------------------------------------
# S0 — границы поиска. Фильтр, не балл (§3.3)
# ---------------------------------------------------------------------------

def apply_scope(candidates: Sequence[CandidateFacts], scope: Scope) -> AdmissionResult:
    """Отсечь всё, что вне объявленных границ. Ни одного слагаемого.

    География здесь и только здесь. Расстояние допускается **исключительно**
    как жёсткий предел радиуса; слагаемым ранжирования оно не становится
    ни на одной стадии (§3.3, канон §9.1). Неизвестная география при явном
    радиусе — исключение, а не «полбалла»: подтвердить попадание в радиус
    нечем.
    """
    admitted: list[CandidateFacts] = []
    excluded: list[ExcludedCandidate] = []
    codes: dict[UUID, frozenset[ReasonCode]] = {}

    include = set(scope.tenant_refs)
    exclude = set(scope.exclude_tenant_refs)

    for facts in candidates:
        cid = facts.ref.id
        granted: set[ReasonCode] = set()

        if include or exclude:
            if facts.tenant_ref is None or (include and facts.tenant_ref not in include) \
                    or facts.tenant_ref in exclude:
                excluded.append(ExcludedCandidate(facts.ref, StageId.S0, ReasonCode.SCOPE_EXCLUDED_OUT_OF_TENANT))
                continue
            if include:
                granted.add(ReasonCode.SCOPE_WITHIN_TENANT)
        if not include and not exclude:
            granted.add(ReasonCode.SCOPE_CROSS_TENANT_ALLOWED)

        if scope.city is not None:
            if facts.city is None or facts.city.strip().casefold() != scope.city.strip().casefold():
                excluded.append(ExcludedCandidate(facts.ref, StageId.S0, ReasonCode.SCOPE_EXCLUDED_OUT_OF_CITY))
                continue
            granted.add(ReasonCode.SCOPE_WITHIN_CITY)

        if scope.radius_km is not None:
            if facts.distance_km is None:
                excluded.append(ExcludedCandidate(facts.ref, StageId.S0, ReasonCode.SCOPE_GEO_UNKNOWN_EXCLUDED))
                continue
            if facts.distance_km > scope.radius_km:
                excluded.append(ExcludedCandidate(facts.ref, StageId.S0, ReasonCode.SCOPE_EXCLUDED_OUT_OF_AREA))
                continue
            granted.add(ReasonCode.SCOPE_WITHIN_REQUESTED_AREA)

        admitted.append(facts)
        codes[cid] = frozenset(granted)

    return AdmissionResult(tuple(admitted), tuple(excluded), codes)


# ---------------------------------------------------------------------------
# S1 — жёсткая допустимость. Компенсации баллом нет (R2)
# ---------------------------------------------------------------------------

def apply_eligibility(
    candidates: Sequence[CandidateFacts],
    request: RecommendationRequest,
    policy: StagePolicy,
) -> AdmissionResult:
    """Допустимое множество. Не прошедший сюда не участвует нигде дальше.

    Fail-closed везде, где признака нет: `UNKNOWN` — не «наверное да».
    Это прямо стоит в §10.2 («признак отсутствует → не проходит») и в общем
    правиле §14 («ни одна ветка не разрешает подставить значение вместо
    UNKNOWN»).

    `safety = STOP` и `safety = UNKNOWN` дают пустое множество: второе —
    тоже fail-closed, потому что «мы не знаем, безопасно ли» не является
    разрешением.
    """
    admitted: list[CandidateFacts] = []
    excluded: list[ExcludedCandidate] = []
    codes: dict[UUID, frozenset[ReasonCode]] = {}
    evidence: dict[UUID, tuple[EvidenceItem, ...]] = {}

    # Перепись собирается ЗДЕСЬ, в том же проходе, что и отказы (§76).
    # Отдельный запрос «сколько у нас непроверенных» отвечал бы на тот же
    # вопрос своими условиями — и разошёлся бы с гейтом в первый же день,
    # когда условие изменят в одном месте из двух.
    tally: dict[MappingStatus, int] = {status: 0 for status in MappingStatus}

    # Список закрывается ЦЕЛИКОМ только когда safety-контекст ЗАПРОСА требует
    # оценки всего ответа: STOP / UNKNOWN (fail-closed по §10.2), либо
    # NOT_APPLICABLE при ответе, персонализированном прошлым опытом человека —
    # тогда ответ есть интерпретация ПРО ЧЕЛОВЕКА, и SafetyResult нужен ему
    # весь (OD §72). Чувствительность отдельного кандидата (услуга требует
    # проверки здоровья) — свойство КАНДИДАТА и закрывает ЕГО, ниже в цикле
    # (DRF-1627, frozen Safety canon: capability/candidate-specific, не
    # глобальный переключатель списка).
    #
    # До DRF-1627 здесь стояло `any(...)` по обоим признакам сразу, и один
    # «Медицинский педикюр» среди 31 кандидата гасил безопасный массаж и
    # маникюр рядом — причём ДО гейта маппинга, так что подтверждение связей
    # полку не открывало.
    safety_blocks_all = request.safety_state in (SafetyState.STOP, SafetyState.UNKNOWN)
    if request.safety_state is SafetyState.NOT_APPLICABLE and _is_personalised(candidates):
        logger.warning(
            "recommendation.safety.not_applicable_refused — заявлено NOT_APPLICABLE, "
            "но выдача персонализирована прошлым опытом человека; fail-closed как при UNKNOWN "
            "(OD §72, контракт §4.1)"
        )
        safety_blocks_all = True
    budget = request.constraints.price_max
    need_is_stated = request.need.is_stated
    hard_preferences, _, _ = effective_preferences(request.preferences)
    hard_by_kind = _by_kind(hard_preferences)

    for facts in candidates:
        cid = facts.ref.id
        granted: set[ReasonCode] = set()
        items: list[EvidenceItem] = []

        # До любых веток: перепись считает УВИДЕННЫХ, а не выживших.
        # Кандидат, выбывший по безопасности или способности, свой статус
        # связи всё равно имеет, и в наблюдаемости он обязан быть виден —
        # иначе числа схлопнутся ровно на тех, из-за кого их и завели.
        tally[facts.mapping_status] = tally.get(facts.mapping_status, 0) + 1

        if safety_blocks_all or facts.safety_blocked:
            excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_SAFETY))
            continue

        if _needs_own_safety_result(facts, request.safety_state):
            # Точечный fail-closed (DRF-1627): у ЭТОГО кандидата услуга требует
            # проверки здоровья, а поверхность SafetyResult не запрашивала.
            # Закрывается он — соседи продолжают конвейер. Снять глобальный
            # отказ и не оставить точечного значило бы открыть то, что
            # закрыто по делу.
            logger.info(
                "recommendation.safety.candidate_unassessed — кандидат %s требует проверки "
                "здоровья, SafetyResult не запрошен (NOT_APPLICABLE); исключён он один (DRF-1627)",
                cid,
            )
            excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_SAFETY))
            continue

        if facts.is_active_offer is not True:
            excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_INACTIVE))
            continue
        granted.add(ReasonCode.ELIG_ACTIVE_OFFER)

        if facts.is_capable is not True:
            excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE))
            continue

        # НАЗВАННАЯ НУЖДА — УСЛОВИЕ ДОПУСТИМОСТИ, а не только порядок.
        #
        # Уточнение, вынужденное реализацией. По §4.4 стадии S2–S6 не
        # исключают, и это верно про СТЕПЕНЬ соответствия: кто подходит
        # точнее, а кто грубее. Но кандидат, про которого соответствие
        # ВООБЩЕ не определилось, при явно названной нужде — это не
        # «подходит хуже», это «не отвечает тому, о чём спросили».
        #
        # Показать его значило бы молча подставить другую услугу
        # (канон §14.4 запрещает прямо), а «непустой недоказанный ответ»
        # хуже пустого честного (§14). Человек, набравший «массаж»,
        # не должен получать маникюр ниже по списку — он должен получать
        # массаж или честное «никого».
        #
        # Способность — она же и есть: мастер, не отвечающий названной
        # нужде, для ЭТОГО запроса не способен, отсюда тот же код.
        #
        # Уровень берётся ТЕМ ЖЕ помощником, что и в S2 (`_effective_match_level`),
        # а не полем фактов напрямую. Иначе провайдер без разрешённой услуги
        # проходил бы S1 как «совпал», а в S2 читался бы как UNDETERMINED
        # по K2 — две стадии, два ответа на один вопрос. Ровно тот разрыв,
        # который мы и убираем этим эпиком.
        if need_is_stated and _effective_match_level(facts) is MatchLevel.UNDETERMINED:
            excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE))
            continue

        eligible, mapping_item = _mapping_admission(facts, policy)
        if not eligible:
            excluded.append(
                ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE)
            )
            continue
        # CAT-10 (чинит C1): связь VERIFIED ещё не значит, что body-care
        # процедуру можно рекомендовать — её конфигурация должна быть готова
        # к скринингу (CAT-6 READY). Гейт на ЧТЕНИИ: состояние может упасть
        # после верификации связи, и это ловится здесь без пересмотра связи.
        # ``None`` — услуга вне Body Care: гейт к ней не относится.
        if facts.config_ready is False:
            excluded.append(
                ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY)
            )
            continue
        # O-1: «только X» — жёсткое условие, и только из ТЕКУЩЕГО запроса.
        # Стоит ПОСЛЕ проверок процедуры и связи: предпочтение работает на
        # допустимых кандидатах и ничего из них не отменяет.
        # Внутри вида — ИЛИ («Анна или Мария»), между видами — И («Анна, и
        # только в этом салоне»). «Только массаж» при неизвестных категориях
        # кандидата исключает: жёсткое условие подтверждается, а не
        # предполагается (как известный бюджет при неизвестной цене ниже).
        if not all(
            any(satisfies_preference(facts, p, need_is_stated=need_is_stated) for p in same_kind)
            for same_kind in hard_by_kind.values()
        ):
            excluded.append(
                ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_PREFERENCE_HARD)
            )
            continue
        granted.add(ReasonCode.ELIG_CAPABILITY_VERIFIED)
        if mapping_item is not None:
            items.append(mapping_item)

        if budget.kind is ConstraintKind.KNOWN:
            if facts.price is None or facts.price > budget.value:
                excluded.append(ExcludedCandidate(facts.ref, StageId.S1, ReasonCode.ELIG_EXCLUDED_BUDGET))
                continue
            granted.add(ReasonCode.ELIG_WITHIN_STATED_BUDGET)

        # CLARIFY: резолвер отрабатывает, но «можно ли рекомендовать»
        # решает трек A, поэтому код «безопасность пройдена» здесь НЕ
        # выдаётся — иначе мы ответим за чужой контракт (§14).
        #
        # NOT_APPLICABLE тоже не даёт этого кода, и это не придирка:
        # «безопасность пройдена» — утверждение о проверке, а проверки
        # не было. Очищать было нечего.
        if request.safety_state is SafetyState.NORMAL:
            granted.add(ReasonCode.ELIG_SAFETY_CLEARED)

        admitted.append(facts)
        codes[cid] = frozenset(granted)
        if items:
            evidence[cid] = tuple(items)

    census = MappingCensus(
        visible=len(candidates),
        recommendation_eligible=len(admitted),
        review_required=tally.get(MappingStatus.REVIEW_REQUIRED, 0),
        unmapped=tally.get(MappingStatus.UNMAPPED, 0),
        not_recommendable=tally.get(MappingStatus.NOT_RECOMMENDABLE, 0),
        unknown=tally.get(MappingStatus.UNKNOWN, 0),
    )
    return AdmissionResult(tuple(admitted), tuple(excluded), codes, evidence, census)


def _is_personalised(candidates: Sequence[CandidateFacts]) -> bool:
    """Персонализирован ли ответ прошлым опытом человека — признак СПИСКА.

    Если хоть один кандидат отобран по прошлым визитам, ответ уже не «что
    есть в каталоге», а «тебе сейчас лучше вот эти» — интерпретация про
    человека, и SafetyResult нужен всему ответу (OD §72). Это единственный
    признак, который закрывает список целиком при NOT_APPLICABLE.

    Проверка здоровья по услуге здесь НЕ учитывается намеренно: это признак
    кандидата, и он обрабатывается точечно (`_needs_own_safety_result`,
    DRF-1627). До DRF-1627 оба признака стояли в одном `any(...)`.
    """
    return any(
        facts.prior_completed_visit or facts.prior_completed_same_category
        for facts in candidates
    )


def _needs_own_safety_result(facts: CandidateFacts, safety_state: SafetyState) -> bool:
    """Требует ли ЭТОТ кандидат SafetyResult, которого у запроса нет.

    Услуга с проверкой здоровья без оценки безопасности — fail-closed для
    этого кандидата (frozen Safety canon: capability/candidate-specific).
    При STOP/UNKNOWN вопрос не стоит — список уже закрыт целиком выше; при
    NORMAL/DEGRADED SafetyResult есть, и его вердикт лежит в
    `facts.safety_blocked`.
    """
    return safety_state is SafetyState.NOT_APPLICABLE and bool(facts.requires_health_check)


def _mapping_admission(facts: CandidateFacts, policy: StagePolicy) -> tuple[bool, EvidenceItem | None]:
    """`recommendation_eligible = (mapping_status == VERIFIED)` — §10.1.

    Исключение владельца (§10.4 вариант «а») реализуется **только** как явная
    политика с версией и **всегда** оставляет след в свидетельстве. Иначе
    повторится история числа `rating`: значение, поставленное как временная
    техническая мера, через месяц читается как проверенный факт.
    """
    if facts.mapping_status is MappingStatus.VERIFIED:
        return True, EvidenceItem(
            kind=EvidenceKind.CAPABILITY_MAPPING,
            value=MappingStatus.VERIFIED,
            strength=EvidenceStrength.CONFIRMED,
            origin=EvidenceOrigin.CURATED_KNOWLEDGE,
            source_ref=facts.source_ref,
        )
    # Второй ветки здесь больше нет. Она пропускала кандидата с любым
    # статусом, помечая его `UNSUBSTANTIATED` — и это было честно ровно
    # до тех пор, пока вопрос владельца оставался открытым. Он ответил
    # (§76): ноль `VERIFIED` не разрешает fallback, иначе статус
    # декоративен. Пустая выдача — штатное состояние с именем, а не повод
    # ослабить признак.
    return False, None


# ---------------------------------------------------------------------------
# S2 — соответствие нужде
# ---------------------------------------------------------------------------

def stage_semantic_fit(candidates: Sequence[CandidateFacts], need: NeedSpec) -> StageOutput:
    """Насколько кандидат отвечает НУЖДЕ. Первая стадия, которая различает.

    K2 (§5.1): провайдера нельзя поставить выше другого «по соответствию
    нужде», если неизвестно, какая его услуга этой нужде отвечает. Поэтому
    провайдер без `matched_service_ref` не поднимается выше уровня
    `GOAL_CATEGORY` — курируемой связи «цель → категории», которая говорит
    о классе услуг, а не о конкретной. Это ровно сегодняшний дефект
    поверхности B: провайдеры упорядочены как будто по соответствию,
    а соответствие не вычислялось.
    """
    if not need.is_stated:
        return StageOutput(
            StageId.S2, active=False,
            inactive_reason="нужда не названа — соответствие не вычисляется, а не равно нулю",
        )

    keys: dict[UUID, float] = {}
    codes: dict[UUID, frozenset[ReasonCode]] = {}
    evidence: dict[UUID, tuple[EvidenceItem, ...]] = {}

    for facts in candidates:
        level = _effective_match_level(facts)
        depth = facts.goal_fit_depth if level is MatchLevel.GOAL_CATEGORY else None
        # DRF-2789 (R0): глубина цели — дробная часть ключа уровня. Она
        # различает только внутри «категории цели» и не может поднять его
        # выше совпадения по названию: максимум 1.4 < 2.
        keys[facts.ref.id] = float(MATCH_RANK[level]) + (
            _GOAL_DEPTH_STEP * depth if depth is not None else 0.0
        )
        level_codes = {MATCH_CODE[level]}
        if depth is not None:
            level_codes |= GOAL_DEPTH_CODES[depth]
        codes[facts.ref.id] = frozenset(level_codes)
        if level is not MatchLevel.UNDETERMINED:
            evidence[facts.ref.id] = (
                EvidenceItem(
                    kind=EvidenceKind.SERVICE_MATCH,
                    value=level,
                    strength=EvidenceStrength.CONFIRMED,
                    origin=(
                        EvidenceOrigin.CURATED_KNOWLEDGE
                        if level is MatchLevel.GOAL_CATEGORY
                        else EvidenceOrigin.DOMAIN_FACT
                    ),
                    source_ref=str(facts.matched_service_ref or facts.matched_goal_category_ref or ""),
                ),
            )

    return StageOutput(StageId.S2, active=True, keys=keys, codes=codes, evidence=evidence)


#: Шаг глубины цели внутри уровня: 0.1. Пять значений глубины (0–4) дают
#: не больше 0.4 — меньше расстояния между соседними уровнями (1.0).
_GOAL_DEPTH_STEP = 0.1

#: Коды объяснения глубины (DRF-2789): класс категории и, если она досталась
#: раскрытием корня, отметка об этом.
GOAL_DEPTH_CODES: dict[int, frozenset[ReasonCode]] = {
    4: frozenset({ReasonCode.MATCH_GOAL_CONFIRMED_CAPABILITY}),
    3: frozenset({ReasonCode.MATCH_GOAL_PRIMARY_CATEGORY}),
    2: frozenset({ReasonCode.MATCH_GOAL_PRIMARY_CATEGORY, ReasonCode.MATCH_GOAL_EXPANDED_CATEGORY}),
    1: frozenset({ReasonCode.MATCH_GOAL_SECONDARY_CATEGORY}),
    0: frozenset({ReasonCode.MATCH_GOAL_SECONDARY_CATEGORY, ReasonCode.MATCH_GOAL_EXPANDED_CATEGORY}),
}


def _effective_match_level(facts: CandidateFacts) -> MatchLevel:
    """Уровень соответствия с поправкой K2 для провайдеров."""
    level = facts.match_level
    if facts.ref.kind is not CandidateKind.PROVIDER:
        return level
    if facts.matched_service_ref is not None:
        return level
    if level is MatchLevel.GOAL_CATEGORY and facts.matched_goal_category_ref is not None:
        return level
    return MatchLevel.UNDETERMINED


# ---------------------------------------------------------------------------
# S3 — транзакционная пригодность
# ---------------------------------------------------------------------------

def stage_transaction_fit(
    candidates: Sequence[CandidateFacts],
    request: RecommendationRequest,
    policy: StagePolicy,
) -> StageOutput:
    """Исполнимо ли это: бронируемость и — по условию — подтверждённое расписание.

    K6: признак доступности включается только когда под ним появились данные
    (подтверждённое расписание у 80% бронируемых мастеров). До этого он
    `INACTIVE`, а не «ноль слотов»: незаполненное расписание не означает
    занятость (§29.5, `UNKNOWN ≠ 0 ≠ false`).

    K5: при заявленном временном окне кандидат с неподтверждённым расписанием
    остаётся в выдаче, но первый ярус ему запрещён.
    """
    wants_time = request.constraints.time_window.kind is ConstraintKind.KNOWN
    use_schedule = wants_time and policy.availability_ranking_enabled
    bookable_known = any(f.is_bookable is not None for f in candidates)

    if not bookable_known and not use_schedule:
        reason = (
            "признак доступности выключен до выполнения условия 80% (K6); "
            "бронируемость не сообщена источником"
        )
        return StageOutput(StageId.S3, active=False, inactive_reason=reason)

    keys: dict[UUID, float] = {}
    codes: dict[UUID, frozenset[ReasonCode]] = {}
    tier_one_forbidden: set[UUID] = set()

    for facts in candidates:
        cid = facts.ref.id
        granted: set[ReasonCode] = set()
        key = 0.0

        if facts.is_bookable is True:
            granted.add(ReasonCode.EXEC_BOOKABLE)
            key += 2.0

        if use_schedule:
            if facts.schedule_state is ScheduleState.CONFIRMED_IN_WINDOW:
                granted.add(ReasonCode.EXEC_SLOT_CONFIRMED_IN_WINDOW)
                key += 1.0
            elif facts.schedule_state is ScheduleState.UNCONFIRMED:
                granted.add(ReasonCode.EXEC_SCHEDULE_UNCONFIRMED)
                tier_one_forbidden.add(cid)

        if facts.price is None and request.constraints.price_max.kind is ConstraintKind.FLEXIBLE:
            granted.add(ReasonCode.EXEC_PRICE_UNKNOWN)

        keys[cid] = key
        if granted:
            codes[cid] = frozenset(granted)

    return StageOutput(
        StageId.S3, active=True, keys=keys, codes=codes,
        tier_one_forbidden=frozenset(tier_one_forbidden),
    )


# ---------------------------------------------------------------------------
# S4 — контекстная персонализация
# ---------------------------------------------------------------------------

def effective_preferences(
    preferences: Sequence[Preference],
) -> tuple[tuple[Preference, ...], tuple[Preference, ...], tuple[Preference, ...]]:
    """(жёсткие текущего запроса, мягкие текущего запроса, мягкие из памяти) — O-1.

    * **Текущий запрос важнее истории — по виду.** Если в запросе назван
      мастер, предпочтение мастера из памяти не учитывается вовсе; память о
      категории при этом работает.
    * **Жёстко отсекает только сказанное сейчас.** «Только Анна» из памяти
      читается как мягкое: устаревшее условие не прячет всех кандидатов.
    * Повторы схлопываются: одно и то же предпочтение дважды не считается.
    """
    current = [p for p in preferences if p.origin is PreferenceOrigin.CURRENT_REQUEST]
    named_now = {p.kind for p in current}
    hard = tuple(dict.fromkeys(p for p in current if p.strength is PreferenceStrength.HARD))
    soft_current = tuple(dict.fromkeys(
        (p.kind, p.ref) for p in current if p.strength is PreferenceStrength.SOFT
    ))
    soft_memory = tuple(dict.fromkeys(
        (p.kind, p.ref) for p in preferences
        if p.origin is PreferenceOrigin.CONFIRMED_MEMORY and p.kind not in named_now
    ))
    as_pref = lambda pairs, origin: tuple(  # noqa: E731 — локальная форма
        Preference(kind=k, ref=r, strength=PreferenceStrength.SOFT, origin=origin) for k, r in pairs
    )
    return (
        hard,
        as_pref(soft_current, PreferenceOrigin.CURRENT_REQUEST),
        as_pref(soft_memory, PreferenceOrigin.CONFIRMED_MEMORY),
    )


def _by_kind(preferences: Sequence[Preference]) -> dict[PreferenceKind, tuple[Preference, ...]]:
    out: dict[PreferenceKind, list[Preference]] = {}
    for p in preferences:
        out.setdefault(p.kind, []).append(p)
    return {k: tuple(v) for k, v in out.items()}


def _candidate_categories(facts: CandidateFacts, *, need_is_stated: bool) -> frozenset[UUID]:
    """Категории, с которыми сверяется предпочтение категории.

    Нужда названа — категории той услуги, которой мастер на неё ответил;
    не названа — категории всех его предложений. Лист и родитель — оба:
    услуги висят на листьях, а клиент называет корень.
    """
    refs = facts.matched_category_refs if need_is_stated else facts.category_refs
    if facts.matched_goal_category_ref is not None:
        refs = refs | {facts.matched_goal_category_ref}
    return refs


def satisfies_preference(
    facts: CandidateFacts, preference: Preference, *, need_is_stated: bool = False,
) -> bool:
    """Отвечает ли кандидат предпочтению — по виду предпочтения."""
    if preference.kind is PreferenceKind.MASTER:
        return facts.ref.id == preference.ref
    if preference.kind is PreferenceKind.SALON:
        return facts.tenant_ref == preference.ref
    if preference.kind is PreferenceKind.CATEGORY:
        return preference.ref in _candidate_categories(facts, need_is_stated=need_is_stated)
    return False  # pragma: no cover — закрытое перечисление


#: Порядок измерений внутри одного происхождения: мастер, салон, категория.
_PREFERENCE_DIMENSIONS = (PreferenceKind.MASTER, PreferenceKind.SALON, PreferenceKind.CATEGORY)


def stage_contextual(
    candidates: Sequence[CandidateFacts],
    preferences: Sequence[Preference] = (),
    *,
    need_is_stated: bool = False,
) -> StageOutput:
    """Прошлый успешный опыт. Только `COMPLETED` — и это не сокращение.

    Канон §10.3: `shown ≠ engaged ≠ booked ≠ completed ≠ liked`. Показ,
    интерес и даже запись прошлым опытом не являются; §9.1 требует «strong
    provenance and current relevance». Сегодняшний
    `_load_history_specialist_ids` читает именно `COMPLETED` — это
    правильное чтение, и оно переиспользуется без изменений.
    """
    _, soft_current, soft_memory = effective_preferences(preferences)
    has_history = any(f.prior_completed_visit or f.prior_completed_same_category for f in candidates)
    if not has_history and not soft_current and not soft_memory:
        return StageOutput(
            StageId.S4, active=False,
            inactive_reason="ни предпочтений клиента, ни завершённых визитов — персонализировать нечем",
        )

    keys: dict[UUID, float] = {}
    lex_keys: dict[UUID, tuple[int, ...]] = {}
    unknown_from: dict[UUID, int] = {}
    codes: dict[UUID, frozenset[ReasonCode]] = {}
    evidence: dict[UUID, tuple[EvidenceItem, ...]] = {}
    now_by_kind, memory_by_kind = _by_kind(soft_current), _by_kind(soft_memory)
    # Первое измерение категории, которое участвует: текущего запроса (2),
    # иначе памяти (3 + 2). Неизвестные категории замораживают с него.
    category_position = (
        _PREFERENCE_DIMENSIONS.index(PreferenceKind.CATEGORY) if PreferenceKind.CATEGORY in now_by_kind
        else len(_PREFERENCE_DIMENSIONS) + _PREFERENCE_DIMENSIONS.index(PreferenceKind.CATEGORY)
        if PreferenceKind.CATEGORY in memory_by_kind
        else None
    )

    def hits(facts: CandidateFacts, by_kind) -> tuple[bool, ...]:
        return tuple(
            any(satisfies_preference(facts, p, need_is_stated=need_is_stated) for p in by_kind.get(kind, ()))
            for kind in _PREFERENCE_DIMENSIONS
        )

    for facts in candidates:
        cid = facts.ref.id
        # §29.4 по измерению: категорий кандидата не знаем — молчит только
        # сравнение по категории и всё младшее. «Мой мастер Анна» (старше)
        # работает, даже если у соседа услуги без категорий.
        if category_position is not None and not _candidate_categories(facts, need_is_stated=need_is_stated):
            unknown_from[cid] = category_position
        # O-1 (DRF-2816): лексикографически ВНУТРИ стадии, без весов и сумм —
        # да/нет по измерениям в закреплённом порядке: текущий запрос
        # (мастер, салон, категория), затем подтверждённая память (то же),
        # затем история визитов. Любой старший признак перевешивает все
        # младшие вместе: два совпадения не «стоят» одного более важного.
        now, remembered = hits(facts, now_by_kind), hits(facts, memory_by_kind)
        pref_codes: set[ReasonCode] = set()
        if any(now):
            pref_codes.add(ReasonCode.CONTEXT_PREFERENCE_CURRENT_REQUEST)
        if any(remembered):
            pref_codes.add(ReasonCode.CONTEXT_PREFERENCE_CONFIRMED_MEMORY)
        history = 2 if (has_history and facts.prior_completed_visit) else (
            1 if (has_history and facts.prior_completed_same_category) else 0
        )
        lex_keys[cid] = tuple(int(bit) for bit in now + remembered) + (history,)
        # То же число одной величиной — для журнала; делит группу ``lex_keys``.
        pref_key = 0
        for bit in now + remembered:
            pref_key = pref_key * 2 + int(bit)
        pref_key *= 3
        if not has_history:
            keys[cid] = float(pref_key)
            codes[cid] = frozenset(pref_codes or {ReasonCode.CONTEXT_NOT_APPLICABLE})
            continue
        if facts.prior_completed_visit:
            keys[cid] = float(pref_key) + 2.0
            codes[cid] = frozenset({ReasonCode.CONTEXT_PRIOR_COMPLETED_VISIT} | pref_codes)
            evidence[cid] = (
                EvidenceItem(
                    kind=EvidenceKind.PRIOR_VISIT,
                    value=True,
                    strength=EvidenceStrength.CONFIRMED,
                    origin=EvidenceOrigin.DOMAIN_FACT,
                    source_ref=facts.source_ref,
                ),
            )
        elif facts.prior_completed_same_category:
            keys[cid] = float(pref_key) + 1.0
            codes[cid] = frozenset({ReasonCode.CONTEXT_PRIOR_SAME_CATEGORY} | pref_codes)
        else:
            keys[cid] = float(pref_key)
            codes[cid] = frozenset(pref_codes or {ReasonCode.CONTEXT_NOT_APPLICABLE})

    return StageOutput(
        StageId.S4, active=True, keys=keys, codes=codes, evidence=evidence,
        lex_keys=lex_keys, unknown_from=unknown_from,
    )


# ---------------------------------------------------------------------------
# S5 — качество
# ---------------------------------------------------------------------------

def stage_quality(candidates: Sequence[CandidateFacts], policy: StagePolicy) -> StageOutput:
    """Вторичное свидетельство качества — и только подтверждённое (§8).

    Кандидат с `UNSUBSTANTIATED` / `WEAK` / `UNKNOWN` для этой стадии
    `INACTIVE`: он **не понижается** (§29.4), а стадия про его группу
    молчит (решение 1 в заголовке модуля).

    Свидетельство с `UNSUBSTANTIATED` при этом **передаётся** потребителю
    вместе с кодом `QUALITY_RATING_UNSUBSTANTIATED_IGNORED` — кодом, который
    прямо говорит «учтено не было». Скрыть его нельзя: потребитель вправе
    показать число справочно и не вправе выдать его за причину.
    """
    keys: dict[UUID, float] = {}
    codes: dict[UUID, frozenset[ReasonCode]] = {}
    evidence: dict[UUID, tuple[EvidenceItem, ...]] = {}
    inactive_for: set[UUID] = set()

    for facts in candidates:
        cid = facts.ref.id
        strength = rating_strength(facts.rating, n_substantiated=policy.n_substantiated)
        item = rating_evidence(
            facts.rating,
            observed_at=facts.rating_observed_at,
            source_ref=facts.source_ref,
            n_substantiated=policy.n_substantiated,
        )
        if strength is EvidenceStrength.CONFIRMED and facts.rating is not None:
            keys[cid] = float(facts.rating.rating)
            codes[cid] = frozenset({ReasonCode.QUALITY_RATING_SUBSTANTIATED})
            evidence[cid] = (item,)
            continue

        inactive_for.add(cid)
        keys[cid] = 0.0
        if strength is EvidenceStrength.UNKNOWN:
            codes[cid] = frozenset({ReasonCode.QUALITY_NO_EVIDENCE})
        else:
            codes[cid] = frozenset({ReasonCode.QUALITY_RATING_UNSUBSTANTIATED_IGNORED})
            evidence[cid] = (item,)

    active = len(inactive_for) < len(candidates)
    return StageOutput(
        StageId.S5,
        active=active,
        inactive_reason=None if active else "подтверждённого свидетельства качества нет ни у одного кандидата",
        keys=keys,
        inactive_for=frozenset(inactive_for),
        codes=codes,
        evidence=evidence,
    )
