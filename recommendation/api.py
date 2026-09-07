"""Публичная поверхность резолвера — **единственный** модуль, который импортируют извне.

Всё, что нужно потребителю, лежит здесь: :func:`resolve`, типы запроса и
ответа, реестр кодов, сила свидетельства. Всё, чего потребителю знать
не положено — ключи стадий, внутренние числа, устройство ярусов, —
живёт в приватных модулях и не экспортируется.

Почему это важнее, чем кажется. До резолвера решение о порядке принимали
три реализации, две из них в одном процессе, и разошлись они не в споре,
а потому, что каждая могла дотянуться до чего угодно и написать свою
формулу рядом. Узкая публичная поверхность — это способ сделать четвёртую
формулу видимой в тот момент, когда её пишут, а не через месяц на экране
у человека.

Две проекции одной границы
--------------------------
* **внутрипроцессная** — прямой вызов :func:`resolve` (домашний экран,
  LLM-контекст: они живут в этом же процессе);
* **межпроцессная** — `POST /api/v1/internal/recommendation/resolve/`
  (бот), тонкое представление над тем же вызовом.

Обе отдают один и тот же неизменяемый :class:`RecommendationDecision`.
Граница — это контракт и точка входа, а не сетевой хоп: если бы границей
был HTTP, для двух потребителей из трёх её бы просто не существовало.

Чего этот модуль не делает
--------------------------
Не сохраняет `Recommendation` (авторитет домена, §6.1), не рисует текст
человеку (§7.3: наружу идут коды, фразу собирает представление), не ходит
в ORM (доменную правду приносит :class:`CandidateSource`).
"""
from __future__ import annotations

from ._evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceOrigin,
    EvidenceStrength,
    RatingValue,
    rating_evidence,
    rating_strength,
)
from ._pipeline import RESOLVER_SPEC_VERSION, TIE_BREAK_POLICY_VERSION, resolve
from ._reason_codes import REGISTRY_VERSION as REASON_CODE_REGISTRY_VERSION
from ._reason_codes import ReasonCode, ReasonCodeInvariantError
from ._stages import StagePolicy
from ._types import (
    FLEXIBLE,
    MATCH_CODE,
    MATCH_RANK,
    UNKNOWN,
    CandidateFacts,
    CandidateKind,
    CandidateRef,
    CandidateSource,
    Constraint,
    ConstraintKind,
    ExcludedCandidate,
    MappingStatus,
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    PolicyPins,
    PolicyVersions,
    RankedCandidate,
    RecommendationDecision,
    RecommendationRequest,
    SafetyState,
    ScheduleState,
    Scope,
    ScopeMode,
    StageActivity,
    StageId,
    StageVerdict,
    Surface,
    UserConstraints,
)

__all__ = [
    # точка входа
    "resolve",
    "StagePolicy",
    # версии — обязаны уезжать в ответ (§6.3)
    "RESOLVER_SPEC_VERSION",
    "TIE_BREAK_POLICY_VERSION",
    "REASON_CODE_REGISTRY_VERSION",
    # запрос
    "RecommendationRequest",
    "Scope",
    "ScopeMode",
    "NeedSpec",
    "NeedOrigin",
    "UserConstraints",
    "Constraint",
    "ConstraintKind",
    "UNKNOWN",
    "FLEXIBLE",
    "SafetyState",
    "Surface",
    "PolicyPins",
    # факты домена
    "CandidateSource",
    "CandidateFacts",
    "CandidateRef",
    "CandidateKind",
    "MappingStatus",
    "MatchLevel",
    "MATCH_RANK",
    "MATCH_CODE",
    "ScheduleState",
    "RatingValue",
    # ответ
    "RecommendationDecision",
    "RankedCandidate",
    "ExcludedCandidate",
    "StageActivity",
    "StageId",
    "StageVerdict",
    "PolicyVersions",
    # свидетельства и коды
    "EvidenceItem",
    "EvidenceKind",
    "EvidenceOrigin",
    "EvidenceStrength",
    "rating_evidence",
    "rating_strength",
    "ReasonCode",
    "ReasonCodeInvariantError",
]
