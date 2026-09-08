"""Типы границы — контракт §4. Всё неизменяемо, всё трёхзначно там, где может не быть данных.

Три правила, которые в этом файле сделаны структурно:

* **`null` не схлопывает `UNKNOWN` и `FLEXIBLE`** (§4.1, канон §16.2).
  «Мне всё равно, когда» и «я не спросил, когда» — разные состояния,
  и разные ответы. Отсюда :class:`Constraint`, а не `value | None`.
* **Сырых баллов в выходе нет** (§4.3, канон §8). У :class:`RankedCandidate`
  нет и не будет поля со счётом: наружу идут `tier`, `rank`, коды и evidence.
  Внутренние ключи стадий живут в приватном конвейере и не покидают модуль.
* **`MODEL_INFERENCE` не существует** как значение `origin` (§4.1):
  утверждение, порождённое моделью, не является свидетельством человека.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, Sequence
from uuid import UUID

from ._evidence import EvidenceItem, RatingValue
from ._reason_codes import ReasonCode


# ---------------------------------------------------------------------------
# Общие перечисления
# ---------------------------------------------------------------------------

class CandidateKind(StrEnum):
    """Что именно рекомендуется. Объявляется явно (контракт §5, K1).

    Сегодняшнее «рекомендуются мастера, а не услуги» перестаёт быть неявным
    свойством реализации и становится объявленным `PROVIDER`.
    """

    SERVICE = "SERVICE"
    OFFER = "OFFER"
    PROVIDER = "PROVIDER"
    SLOT = "SLOT"


class Surface(StrEnum):
    """Канал предъявления. Существует для аудита и выбора `k` — и больше ни для чего.

    Читать `surface` внутри стадий S0–S6 **запрещено** (§9.5): один запрос
    обязан давать одно решение независимо от поверхности.
    """

    BOT_CHAT = "BOT_CHAT"
    MINIAPP_HOME = "MINIAPP_HOME"
    MINIAPP_CATALOG = "MINIAPP_CATALOG"


class ScopeMode(StrEnum):
    SALON = "SALON"
    MARKETPLACE = "MARKETPLACE"


class SafetyState(StrEnum):
    """Состояние безопасности запроса. Решение владельца 08.09.2026 (OD §72).

    `UNKNOWN` и `NOT_APPLICABLE` — РАЗНЫЕ состояния, и путать их запрещено::

        UNKNOWN                          NOT_APPLICABLE
        оценка ПРИМЕНИМА,                поверхность НЕ выполняет
        но данных для неё нет            safety-sensitive решение
              |                                  |
         fail-closed                     гейт не применяется

    `NOT_APPLICABLE` определяется **типом поверхности до выполнения
    решения**, а не отсутствием данных. Отсутствующее состояние
    не превращается в него никогда: `payload.get("safety") or
    NOT_APPLICABLE` — fail-open дыра, названная владельцем поимённо
    и запрещённая тестом-сторожем.
    """

    NORMAL = "NORMAL"
    CLARIFY = "CLARIFY"
    CAUTION = "CAUTION"
    STOP = "STOP"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class NeedOrigin(StrEnum):
    """Откуда взялась нужда. `MODEL_INFERENCE` здесь нет намеренно (§4.1)."""

    USER_EXPLICIT = "USER_EXPLICIT"
    USER_CLICK = "USER_CLICK"
    GOAL = "GOAL"
    MEMORY = "MEMORY"


class ConstraintKind(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    FLEXIBLE = "FLEXIBLE"


class MappingStatus(StrEnum):
    """Статус канонического маппинга. `recommendation_eligible = (VERIFIED)` (§10.1).

    `UNKNOWN` — не «наверное можно»: отсутствие признака тоже не даёт
    права рекомендовать (fail-closed).
    """

    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNMAPPED = "UNMAPPED"
    UNKNOWN = "UNKNOWN"


class MatchLevel(StrEnum):
    """Насколько кандидат отвечает нужде. Порядок значимости задан явно ниже.

    `UNDETERMINED` — не «не подходит», а «соответствие не вычислялось»
    (нужда не названа, канонического слоя нет). Понижать за это нельзя:
    отсутствие данных не наказывается (§29.4).
    """

    SERVICE_EXACT = "SERVICE_EXACT"
    SERVICE_PARTIAL = "SERVICE_PARTIAL"
    CAPABILITY_ONLY = "CAPABILITY_ONLY"
    GOAL_CATEGORY = "GOAL_CATEGORY"
    UNDETERMINED = "UNDETERMINED"


#: Ранг уровней соответствия: больше — лучше. Отдельная таблица, а не
#: порядок членов перечисления: порядок объявления — не контракт, а таблица —
#: контракт, и её видно в diff'е.
MATCH_RANK: dict[MatchLevel, int] = {
    MatchLevel.SERVICE_EXACT: 4,
    MatchLevel.SERVICE_PARTIAL: 3,
    MatchLevel.CAPABILITY_ONLY: 2,
    MatchLevel.GOAL_CATEGORY: 1,
    MatchLevel.UNDETERMINED: 0,
}

#: Код, которым объясняется каждый уровень соответствия.
MATCH_CODE: dict[MatchLevel, ReasonCode] = {
    MatchLevel.SERVICE_EXACT: ReasonCode.MATCH_SERVICE_EXACT,
    MatchLevel.SERVICE_PARTIAL: ReasonCode.MATCH_SERVICE_PARTIAL,
    MatchLevel.CAPABILITY_ONLY: ReasonCode.MATCH_CAPABILITY_ONLY,
    MatchLevel.GOAL_CATEGORY: ReasonCode.MATCH_GOAL_CATEGORY,
    MatchLevel.UNDETERMINED: ReasonCode.MATCH_UNDETERMINED,
}


class ScheduleState(StrEnum):
    """Третье состояние расписания — решение владельца §29.5.

    «Неизвестное расписание нельзя считать ни свободным, ни занятым».
    Поэтому `UNCONFIRMED` — самостоятельное значение, а не `False`.
    """

    CONFIRMED_IN_WINDOW = "CONFIRMED_IN_WINDOW"
    CONFIRMED_OUT_OF_WINDOW = "CONFIRMED_OUT_OF_WINDOW"
    UNCONFIRMED = "UNCONFIRMED"


class StageId(StrEnum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"


class StageVerdict(StrEnum):
    """Что стадия сделала с кандидатом (контракт §4.3, R3).

    `TIED` — «стадия работала и не различила», `INACTIVE` — «стадия не
    работала». Разница нужна треку A: во втором случае различимость можно
    поднять, добавив данных, в первом — нельзя.
    """

    DISTINGUISHED = "DISTINGUISHED"
    TIED = "TIED"
    INACTIVE = "INACTIVE"


# ---------------------------------------------------------------------------
# Трёхзначное ограничение
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Constraint:
    """`KNOWN(v)` | `UNKNOWN` | `FLEXIBLE` — и это не `value | None`.

    `UNKNOWN` — мы не знаем; `FLEXIBLE` — человек сказал «неважно».
    Схлопывание их в `null` стирает разницу между «не спросили» и
    «спросили, и ответ — всё равно», после чего первое молча выдаётся
    за второе.
    """

    kind: ConstraintKind
    value: Any = None

    def __post_init__(self) -> None:
        if self.kind is ConstraintKind.KNOWN and self.value is None:
            raise ValueError("KNOWN без значения — это UNKNOWN, а не KNOWN")
        if self.kind is not ConstraintKind.KNOWN and self.value is not None:
            raise ValueError(f"{self.kind} со значением {self.value!r}: значение бывает только у KNOWN")

    @classmethod
    def known(cls, value: Any) -> Constraint:
        return cls(ConstraintKind.KNOWN, value)

    @classmethod
    def unknown(cls) -> Constraint:
        return cls(ConstraintKind.UNKNOWN)

    @classmethod
    def flexible(cls) -> Constraint:
        return cls(ConstraintKind.FLEXIBLE)

    @property
    def is_known(self) -> bool:
        return self.kind is ConstraintKind.KNOWN


UNKNOWN = Constraint.unknown()
FLEXIBLE = Constraint.flexible()


# ---------------------------------------------------------------------------
# Вход резолвера
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateRef:
    kind: CandidateKind
    id: UUID


@dataclass(frozen=True)
class Scope:
    """Границы поиска. **Фильтр, не балл** (§3.3).

    `tenant_refs` — множество, а не одна ссылка, и это уточнение контракта
    §4.1, сделанное при исполнении, а не продуктовое решение. Причина:
    полка «твои салоны» — это scope по нескольким тенантам из истории
    человека, и без множественного scope поверхности пришлось бы делать
    несколько вызовов и **сливать их порядок самостоятельно** — то есть
    держать политику. Один вызов со scope сохраняет единственного
    авторитета; несколько вызовов с последующим слиянием его теряют.

    `SALON` требует ровно одного тенанта — режим «внутри салона» (канон
    §14.6). `MARKETPLACE` допускает ноль (весь маркетплейс) или список как
    явное сужение, плюс `exclude_tenant_refs` для обратного сужения
    («всё, кроме тех, где человек уже был»).
    """

    mode: ScopeMode
    tenant_refs: tuple[UUID, ...] = ()
    exclude_tenant_refs: tuple[UUID, ...] = ()
    city: str | None = None          # None = UNKNOWN
    radius_km: float | None = None   # None = UNSET, и это НЕ ноль

    def __post_init__(self) -> None:
        if self.mode is ScopeMode.SALON and len(self.tenant_refs) != 1:
            raise ValueError("SALON требует ровно одного tenant_ref (§4.1)")
        if self.mode is ScopeMode.SALON and self.exclude_tenant_refs:
            raise ValueError("exclude_tenant_refs бессмысленно внутри одного салона")
        if self.radius_km is not None and self.radius_km <= 0:
            raise ValueError("radius_km задан — значит положителен; отсутствие радиуса выражается None (UNSET)")
        overlap = set(self.tenant_refs) & set(self.exclude_tenant_refs)
        if overlap:
            raise ValueError(f"тенант одновременно включён и исключён: {sorted(map(str, overlap))}")


@dataclass(frozen=True)
class NeedSpec:
    """Что человеку нужно. `origin` обязателен и не бывает модельным."""

    origin: NeedOrigin
    capability_refs: tuple[UUID, ...] = ()
    canonical_service_refs: tuple[UUID, ...] = ()
    goal_key: str | None = None
    raw_text: str | None = None

    @property
    def is_stated(self) -> bool:
        """Названа ли нужда хоть чем-нибудь.

        Если нет — S2 не может различать (K2), и это честно объявляется
        `INACTIVE`, а не подменяется нулевым баллом для всех.
        """
        return bool(self.capability_refs or self.canonical_service_refs or self.goal_key or self.raw_text)


@dataclass(frozen=True)
class UserConstraints:
    price_max: Constraint = UNKNOWN
    time_window: Constraint = UNKNOWN
    provider_ref: Constraint = UNKNOWN


@dataclass(frozen=True)
class PolicyPins:
    """Зафиксированные версии политик — для воспроизведения решения (§6.3)."""

    stage_policy_version: str
    catalog_mapping_version: str | None = None
    safety_policy_version: str | None = None


@dataclass(frozen=True)
class RecommendationRequest:
    """Вход резолвера — контракт §4.1."""

    request_id: str
    subject_ref: str
    surface: Surface
    scope: Scope
    need: NeedSpec
    constraints: UserConstraints = field(default_factory=UserConstraints)
    safety_state: SafetyState = SafetyState.UNKNOWN
    tie_break_seed: str | None = None
    k: int = 3
    policy_pins: PolicyPins | None = None

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError("k — сколько будет показано; ноль показанных это не запрос, а его отсутствие")


# ---------------------------------------------------------------------------
# Факты домена — вход стадий
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateFacts:
    """Правда домена об одном кандидате. Политики здесь нет — только факты.

    Поставляет :class:`CandidateSource` (бэкенд Ayla, `AUTHORITATIVE_DOMAIN`).
    Резолвер читает эти поля и **не ходит в ORM сам**: это и есть та черта,
    по которой модуль отделится в `ayla-ai-core` после пилота, не переписывая
    ни одной стадии.

    Все поля, которых может не быть, трёхзначны: `None` означает `UNKNOWN`
    и **нигде не превращается в 0 или False**. Сегодня в коде живут два
    обратных примера — `_score_distance` возвращает `0.5` при неизвестной
    географии, `_score_availability` считает незаполненное расписание
    отсутствием слотов; оба схлопывают UNKNOWN в число.
    """

    ref: CandidateRef
    tenant_ref: UUID | None = None
    city: str | None = None
    distance_km: float | None = None

    # -- S1: жёсткая допустимость -------------------------------------------
    is_active_offer: bool | None = None
    is_capable: bool | None = None
    mapping_status: MappingStatus = MappingStatus.UNKNOWN
    safety_blocked: bool = False
    #: Услуга кандидата требует проверки здоровья (`resolved_requires_health_check`).
    #: Не запрет сам по себе — но признак того, что выдача КАСАЕТСЯ здоровья,
    #: а значит заявление `NOT_APPLICABLE` для неё неправомерно (§4.1).
    requires_health_check: bool = False
    price: Decimal | None = None

    # -- S2: соответствие нужде ---------------------------------------------
    match_level: MatchLevel = MatchLevel.UNDETERMINED
    matched_service_ref: UUID | None = None
    matched_goal_category_ref: UUID | None = None

    # -- S3: транзакционная пригодность -------------------------------------
    is_bookable: bool | None = None
    schedule_state: ScheduleState = ScheduleState.UNCONFIRMED

    # -- S4: контекст --------------------------------------------------------
    prior_completed_visit: bool = False
    prior_completed_same_category: bool = False

    # -- S5: качество --------------------------------------------------------
    rating: RatingValue | None = None
    rating_observed_at: datetime | None = None
    source_ref: str | None = None


class CandidateSource(Protocol):
    """Порт к доменной правде. Отдаёт множество, **не порядок**.

    Реализация живёт на стороне домена (T6/T9/T12) и обязана вернуть
    кандидатов, попадающих в объявленный scope. Порядок возвращаемой
    последовательности значения не имеет и резолвером игнорируется:
    кандидаты канонически пересортировываются до первой стадии, иначе
    источник смог бы влиять на выдачу, оставаясь «просто источником», —
    то есть стать четвёртым авторитетом.
    """

    def fetch(self, *, scope: Scope, need: NeedSpec) -> Sequence[CandidateFacts]:
        ...


# ---------------------------------------------------------------------------
# Выход резолвера
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RankedCandidate:
    """Кандидат в выдаче. Без единого сырого балла (§4.3, канон §8).

    `tier` нормативен: равный `tier` означает **неразличённых** кандидатов.
    Поверхность обязана уметь показать их как равноправных и **не вправе**
    называть первого из яруса лучшим (решение владельца §29.3).
    """

    candidate_ref: CandidateRef
    rank: int
    tier: int
    reason_codes: tuple[ReasonCode, ...]
    evidence: tuple[EvidenceItem, ...] = ()
    stage_verdicts: dict[StageId, StageVerdict] = field(default_factory=dict)


@dataclass(frozen=True)
class ExcludedCandidate:
    """Почему кандидата нет. Только S0/S1: S2–S6 не исключают (§4.4)."""

    candidate_ref: CandidateRef
    excluded_at_stage: StageId
    reason_code: ReasonCode


@dataclass(frozen=True)
class StageActivity:
    """`ACTIVE` | `INACTIVE(reason)` для стадии в целом (§4.2, R4)."""

    stage: StageId
    active: bool
    reason: str | None = None


@dataclass(frozen=True)
class PolicyVersions:
    """Без версий `Recommendation` перестаёт быть свидетельством о решении (§6.3)."""

    resolver_spec_version: str
    stage_policy_version: str
    reason_code_registry_version: str
    catalog_mapping_version: str | None = None
    safety_policy_version: str | None = None
    tie_break_policy_version: str | None = None


@dataclass(frozen=True)
class RecommendationDecision:
    """Результат работы резолвера — контракт §4.2. Резолвером НЕ сохраняется.

    Персистенция (`Recommendation`, `recommendation_id`, lineage) —
    авторитет домена (§6.1). То, что на пилоте резолвер и домен в одном
    процессе, этого не отменяет.
    """

    decision_id: str
    request_id: str
    ordered: tuple[RankedCandidate, ...]
    excluded: tuple[ExcludedCandidate, ...]
    stage_activity: tuple[StageActivity, ...]
    reason_codes: tuple[ReasonCode, ...]
    policy_versions: PolicyVersions
    computed_at: datetime

    @property
    def is_empty(self) -> bool:
        return not self.ordered
