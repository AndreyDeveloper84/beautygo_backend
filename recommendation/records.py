"""Единственный писатель Recommendation records — с проверкой минимума §5 и провенанса.

Мозг (срез 6) отдаёт сюда уже принятое решение как ``RecommendationSetInput``;
здесь — только проверка формы и запись одной транзакцией. Ни одной ветки
выбора NBA: если решение неполно, отказ ``RecordInvalid`` с именем поля, а
не догадка. Никаких LLM, ранжирования, кандидатов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from recommendation.models import (
    ACTIONABILITY_TTL,
    RECORD_SCHEMA_VERSION,
    Recommendation,
    RecommendationEvent,
    RecommendationSet,
)


class RecordInvalid(ValueError):
    """Решение не соответствует минимуму пилота (§5) или провенанс неполон."""


@dataclass(frozen=True)
class PolicyVersions:
    decision_policy: str
    taxonomy: str
    safety_policy: str
    catalog_mapping: str
    presentation_policy: str

    def missing(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if not str(v or "").strip()]


@dataclass(frozen=True)
class RecommendationInput:
    role: str                                  #: primary | alternative
    direction_code: str
    family: str
    target_outcomes: list[str]
    result_status: str
    readiness_state: str
    reason_codes: list[str]
    evidence_refs: list[dict[str, Any]]
    explanation: dict[str, Any]               #: {displayable, user_visible_reasons, internal_only}
    safety_evaluation_ref: dict[str, Any]     #: {state, rule_id, policy_version, evidence_ref, activated_at}
    context_snapshot_ref: dict[str, Any]      #: {snapshot_id, snapshot_version, content_digest}
    consent_evaluation_ref: dict[str, Any] = field(default_factory=dict)
    memory_snapshot_ref: dict[str, Any] | None = None
    execution_mapping_snapshot_ref: dict[str, Any] | None = None
    transaction_snapshot_ref: dict[str, Any] | None = None
    rerank_reason: str = ""
    parent_id: UUID | None = None
    supersedes_id: UUID | None = None


@dataclass(frozen=True)
class RecommendationSetInput:
    subject_ref: str
    intent_id: str
    versions: PolicyVersions
    primary: RecommendationInput
    alternatives: tuple[RecommendationInput, ...] = ()
    semantic_resolution_ref: str = ""


_SNAPSHOT_KEYS = {"snapshot_id", "snapshot_version", "content_digest"}
_SAFETY_KEYS = {"state", "rule_id", "policy_version", "evidence_ref", "activated_at"}


def _check_record(inp: RecommendationInput, label: str) -> None:
    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise RecordInvalid(f"{label}: {msg}")

    need(inp.role in Recommendation.Role.values, f"role {inp.role!r} не из primary|alternative")
    need(bool(inp.direction_code.strip()), "direction_code пуст — decision_subject обязателен (B2)")
    need(inp.family in Recommendation.Family.values,
         f"family {inp.family!r} не из ADDRESS/SUPPORT/RECOVER/OBSERVE (B9)")
    need(isinstance(inp.target_outcomes, list), "target_outcomes — список")
    need(inp.result_status in Recommendation.ResultStatus.values, f"result_status {inp.result_status!r}")
    need(inp.readiness_state in Recommendation.ReadinessState.values, f"readiness_state {inp.readiness_state!r}")
    need(isinstance(inp.reason_codes, list) and len(inp.reason_codes) > 0,
         "reason_codes пуст — каждое решение несёт reason codes (канон v1.1 §8)")
    need(isinstance(inp.evidence_refs, list), "evidence_refs — список")
    need(isinstance(inp.explanation.get("displayable"), bool), "explanation.displayable обязателен (owner 2026-07-29)")
    need(_SAFETY_KEYS <= set(inp.safety_evaluation_ref),
         f"safety_evaluation_ref без {_SAFETY_KEYS - set(inp.safety_evaluation_ref)}")
    need(_SNAPSHOT_KEYS <= set(inp.context_snapshot_ref),
         "context_snapshot_ref — ссылка {snapshot_id, snapshot_version, content_digest} (§7)")
    for name in ("memory_snapshot_ref", "execution_mapping_snapshot_ref", "transaction_snapshot_ref"):
        ref = getattr(inp, name)
        need(ref is None or _SNAPSHOT_KEYS <= set(ref), f"{name} — ссылка на снимок, не копия (B10)")
    if inp.role == Recommendation.Role.ALTERNATIVE:
        need(inp.rerank_reason in Recommendation.RerankReason.values,
             "alternative без rerank_reason (канон v1.1 §10.2)")
    else:
        need(inp.rerank_reason == "" and inp.parent_id is None, "у primary нет parent/rerank_reason")


def _build(inp: RecommendationInput, rset: RecommendationSet, versions: PolicyVersions, now: datetime,
           parent: Recommendation | None) -> Recommendation:
    return Recommendation(
        recommendation_set=rset, role=inp.role, parent=parent, rerank_reason=inp.rerank_reason,
        supersedes_id=inp.supersedes_id,
        direction_code=inp.direction_code, target_outcomes=list(inp.target_outcomes), family=inp.family,
        result_status=inp.result_status, readiness_state=inp.readiness_state,
        reason_codes=list(inp.reason_codes), evidence_refs=list(inp.evidence_refs),
        explanation=dict(inp.explanation), safety_evaluation_ref=dict(inp.safety_evaluation_ref),
        consent_evaluation_ref=dict(inp.consent_evaluation_ref),
        context_snapshot_ref=dict(inp.context_snapshot_ref), memory_snapshot_ref=inp.memory_snapshot_ref,
        execution_mapping_snapshot_ref=inp.execution_mapping_snapshot_ref,
        transaction_snapshot_ref=inp.transaction_snapshot_ref,
        decision_policy_version=versions.decision_policy, taxonomy_version=versions.taxonomy,
        safety_policy_version=versions.safety_policy, catalog_mapping_version=versions.catalog_mapping,
        presentation_policy_version=versions.presentation_policy,
        record_schema_version=RECORD_SCHEMA_VERSION,
        created_at=now, actionable_until=now + ACTIONABILITY_TTL,
    )


def persist(inp: RecommendationSetInput, *, now: datetime | None = None) -> RecommendationSet:
    """Записать выдачу: set + primary + ≤2 alternatives + событие `created` на каждую.

    Отказ до первой записи, если: версии политик неполны; primary не primary;
    alternatives > 2 или не alternative; любая запись не проходит минимум §5.
    """
    now = now or timezone.now()
    missing = inp.versions.missing()
    if missing:
        raise RecordInvalid("провенанс неполон — версии политик пусты: " + ", ".join(missing))
    if inp.primary.role != Recommendation.Role.PRIMARY:
        raise RecordInvalid("primary.role должен быть primary")
    if len(inp.alternatives) > 2:
        raise RecordInvalid(f"alternatives: {len(inp.alternatives)} > 2 (Killer PRD §5.1; контракт §10)")
    _check_record(inp.primary, "primary")
    for i, alt in enumerate(inp.alternatives, start=1):
        if alt.role != Recommendation.Role.ALTERNATIVE:
            raise RecordInvalid(f"alternative[{i}].role должен быть alternative")
        _check_record(alt, f"alternative[{i}]")

    with transaction.atomic():
        rset = RecommendationSet(
            subject_ref=inp.subject_ref, intent_id=inp.intent_id,
            semantic_resolution_ref=inp.semantic_resolution_ref, created_at=now,
        )
        rset.save()
        primary = _build(inp.primary, rset, inp.versions, now, parent=None)
        primary.save()
        _event(primary, RecommendationEvent.Kind.CREATED, now, producer="Recommendation")
        for alt in inp.alternatives:
            parent = primary if alt.parent_id is None else Recommendation.objects.get(pk=alt.parent_id)
            rec = _build(alt, rset, inp.versions, now, parent=parent)
            rec.save()
            _event(rec, RecommendationEvent.Kind.CREATED, now, producer="Recommendation")
    return rset


def _event(rec: Recommendation, kind: str, at: datetime, *, producer: str, payload: dict | None = None):
    ev = RecommendationEvent(recommendation=rec, kind=kind, occurred_at=at, producer=producer, payload=payload or {})
    ev.save()
    return ev


#: Кто вправе производить какое событие (контракт v1.0 §15).
EVENT_PRODUCERS = {
    RecommendationEvent.Kind.CREATED: "Recommendation",
    RecommendationEvent.Kind.PRESENTED: "Channel Delivery / Interaction",
    RecommendationEvent.Kind.EXPLANATION_REQUESTED: "Channel Delivery / Interaction",
    RecommendationEvent.Kind.ALTERNATIVE_REQUESTED: "Channel Delivery / Interaction",
    RecommendationEvent.Kind.ENGAGED: "Channel Delivery / Interaction",
    RecommendationEvent.Kind.BOOKING_INTENT_CREATED: "Booking / Handoff",
}


def record_event(recommendation: Recommendation, kind: str, *, payload: dict | None = None,
                 at: datetime | None = None) -> RecommendationEvent:
    """Append-only факт по записи. `accepted`/`declined` — не события (B8): отказ по имени."""
    if kind in ("recommendation.accepted", "recommendation.declined", "accepted", "declined"):
        raise RecordInvalid(
            f"{kind}: события не существует (B8) — взаимодействие = recommendation.engaged, "
            "переход к исполнению = booking_intent.created, отказ = reaction REJECTED"
        )
    if kind not in RecommendationEvent.Kind.values:
        raise RecordInvalid(f"{kind}: неизвестное событие; допустимы {list(RecommendationEvent.Kind.values)}")
    if kind == RecommendationEvent.Kind.CREATED:
        raise RecordInvalid("recommendation.created пишет только persist() — с сохранением записи")
    payload = dict(payload or {})
    payload.setdefault("recommendation_id", str(recommendation.pk))
    payload.setdefault("recommendation_set_id", str(recommendation.recommendation_set_id))
    return _event(recommendation, kind, at or timezone.now(), producer=EVENT_PRODUCERS[kind], payload=payload)
