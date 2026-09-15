"""Единственный писатель Recommendation records — с проверкой минимума §5 и провенанса.

Мозг (срез 6) отдаёт сюда уже принятое решение как ``RecommendationSetInput``;
здесь — только проверка формы и запись одной транзакцией. Ни одной ветки
выбора NBA: если решение неполно, отказ ``RecordInvalid`` с именем поля, а
не догадка. Никаких LLM, ранжирования, кандидатов.

DRF-1905 — исход на уровне набора (§32)
--------------------------------------

Исход прохода, готовность, вердикт безопасности, снимок контекста и версии
политик — одно на проход и пишутся в набор. ``SAFETY_BOUNDARY`` и
``INSUFFICIENT_CONTEXT`` «не являются NBA»: у такого набора нет ни primary, ни
alternatives, а ``reason_codes`` / ``evidence_refs`` / ``explanation`` набора
объясняют, почему NBA нет. У NBA-исхода primary обязателен; у каждого варианта
свои ``reason_codes`` / ``evidence_refs`` / ``explanation`` (WHY альтернативы, C04.2).

Проверяются **значения**, а не только ключи: ``readiness_state`` и
``safety_evaluation_ref.state`` — ровно из словарей (верхний регистр); строчное
``"blocked"`` — отказ по имени поля, каталог не нормализует за вызывающего.

DRF-1906 — снимок контекста
---------------------------

Снимок приходит **содержимым** (``ContextSnapshotInput``), а не ссылкой: схему,
закрытые словари и класс здоровья проверяет ``recommendation.snapshots``,
digest каталог пересчитывает и сравнивает с присланным; строка снимка и набор с
FK на неё пишутся одной транзакцией. Ссылку §7 строит каталог (``as_ref``).

``explanation.internal_only`` — только коды формы reason_codes
(:data:`CODE_FORM`): текст здесь не хранится, у записи его некому стирать по
отдельности (решение главного окна 15.09; стирание при удалении — DRF-1909).
"""
from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from recommendation import snapshots
from recommendation._types import SafetyState
from recommendation.models import (
    ACTIONABILITY_TTL,
    NO_NBA_STATUSES,
    RECORD_SCHEMA_VERSION,
    ContextSnapshot,
    ExecutionMode,
    ReadinessState,
    Recommendation,
    RecommendationEvent,
    RecommendationSet,
    ResultStatus,
)

#: Длина колонок версий (``CharField(max_length=32)``): длиннее — отказ по имени, а не обрезка.
VERSION_MAX_LENGTH = 32

#: Форма кода reason_codes (``_reason_codes.ReasonCode``: значение = имя, верхний регистр).
#: Этой формы — и только её — принимает ``explanation.internal_only``.
CODE_FORM = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


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

    def too_long(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if len(str(v or "")) > VERSION_MAX_LENGTH]


@dataclass(frozen=True)
class RecommendationInput:
    """Вариант NBA. Исход прохода, готовность, безопасность, снимок и версии — у набора (DRF-1905)."""

    role: str                                  #: primary | alternative
    direction_code: str
    family: str
    target_outcomes: list[str]
    reason_codes: list[str]
    evidence_refs: list[dict[str, Any]]
    explanation: dict[str, Any]               #: {displayable, user_visible_reasons, internal_only}
    consent_evaluation_ref: dict[str, Any] = field(default_factory=dict)
    memory_snapshot_ref: dict[str, Any] | None = None
    execution_mapping_snapshot_ref: dict[str, Any] | None = None
    transaction_snapshot_ref: dict[str, Any] | None = None
    rerank_reason: str = ""
    parent_id: UUID | None = None
    supersedes_id: UUID | None = None


@dataclass(frozen=True)
class ContextSnapshotInput:
    """Снимок хода содержимым (DRF-1906). Схему, словари и digest проверяет каталог; ссылку строит он же."""

    snapshot_version: str
    content_digest: str                       #: sha256 канонического JSON, hex
    content: dict[str, Any]


@dataclass(frozen=True)
class RecommendationSetInput:
    subject_ref: str
    intent_id: str
    versions: PolicyVersions
    #: Исход прохода (§32). SAFETY_BOUNDARY / INSUFFICIENT_CONTEXT — без primary и alternatives.
    result_status: str
    readiness_state: str
    reason_codes: list[str]
    evidence_refs: list[dict[str, Any]]
    explanation: dict[str, Any]
    safety_evaluation_ref: dict[str, Any]     #: {state, rule_id, policy_version, evidence_ref, activated_at}
    context_snapshot: ContextSnapshotInput
    primary: RecommendationInput | None = None
    alternatives: tuple[RecommendationInput, ...] = ()
    semantic_resolution_ref: str = ""
    #: C1: SHADOW по умолчанию — живым набор называет вызывающий явно.
    execution_mode: str = ExecutionMode.SHADOW
    #: {conversation_id, trace_id} — ход диалога (DRF-1754); пусто допустимо (внедиалоговый расчёт).
    conversation_ref: dict[str, Any] = field(default_factory=dict)


_SNAPSHOT_KEYS = {"snapshot_id", "snapshot_version", "content_digest"}
_SAFETY_KEYS = {"state", "rule_id", "policy_version", "evidence_ref", "activated_at"}


def _need(label: str):
    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise RecordInvalid(f"{label}: {msg}")
    return need


def _check_grounds(need, reason_codes, evidence_refs, explanation) -> None:
    """reason_codes / evidence_refs / explanation — одинаковое правило для набора и варианта."""
    need(isinstance(reason_codes, list) and len(reason_codes) > 0,
         "reason_codes пуст — каждое решение несёт reason codes (канон v1.1 §8)")
    need(isinstance(evidence_refs, list), "evidence_refs — список")
    for j, ev in enumerate(evidence_refs if isinstance(evidence_refs, list) else []):
        # Форма элемента типизирована: {source, ref, said_at?}. Словарь `source`
        # (user_stated/confirmed_memory/policy/safety/journey — контракт §12;
        # conversation/anketa/operator/catalog — мозг) здесь НЕ замыкается:
        # свести два словаря — дело контракта, не хранилища. Пустые — отказ.
        need(isinstance(ev, dict) and str(ev.get("source", "")).strip() != "" and str(ev.get("ref", "")).strip() != "",
             f"evidence_refs[{j}] — {{source, ref, said_at?}} с непустыми source и ref "
             "(§105/§145: reason без evidence не печатается)")
    need(isinstance(explanation, dict) and isinstance(explanation.get("displayable"), bool),
         "explanation.displayable обязателен (owner 2026-07-29)")
    # internal_only — только коды (решение главного окна 15.09): текст сказанного сюда
    # не пишется. Значение в отказе не повторяется — оно и есть то, чему здесь не место.
    internal = explanation.get("internal_only", [])
    need(isinstance(internal, list), "explanation.internal_only — список кодов")
    for j, code in enumerate(internal):
        need(isinstance(code, str) and CODE_FORM.match(code) is not None,
             f"explanation.internal_only[{j}] — не код формы reason_codes ({CODE_FORM.pattern}): "
             "текст здесь не хранится")


def _check_set(inp: RecommendationSetInput) -> None:
    need = _need("набор")
    missing = inp.versions.missing()
    if missing:
        raise RecordInvalid("провенанс неполон — версии политик пусты: " + ", ".join(missing))
    too_long = inp.versions.too_long()
    if too_long:
        raise RecordInvalid(f"версии политик длиннее {VERSION_MAX_LENGTH} знаков: " + ", ".join(too_long))
    need(inp.result_status in ResultStatus.values, f"result_status {inp.result_status!r} не из §32")
    need(inp.readiness_state in ReadinessState.values,
         f"readiness_state {inp.readiness_state!r} не из {list(ReadinessState.values)} (верхний регистр)")
    _check_grounds(need, inp.reason_codes, inp.evidence_refs, inp.explanation)
    need(isinstance(inp.safety_evaluation_ref, dict) and _SAFETY_KEYS <= set(inp.safety_evaluation_ref),
         f"safety_evaluation_ref без {_SAFETY_KEYS - set(inp.safety_evaluation_ref or {})}")
    state = (inp.safety_evaluation_ref or {}).get("state")
    need(state in {s.value for s in SafetyState},
         f"safety_evaluation_ref.state {state!r} не из {[s.value for s in SafetyState]} (верхний регистр)")
    snap = inp.context_snapshot
    need(isinstance(snap, ContextSnapshotInput),
         "context_snapshot — снимок содержимым {snapshot_version, content_digest, content} (§7, DRF-1906)")
    try:
        snapshots.check_content(snap.content, snapshot_version=snap.snapshot_version)
    except snapshots.SnapshotInvalid as exc:
        raise RecordInvalid(f"набор: context_snapshot: {exc}") from exc
    # Равенство пересчитанному hexdigest само держит и форму: отдельная проверка формы была бы без сторожа.
    need(snap.content_digest == snapshots.content_digest(snap.content),
         "context_snapshot.content_digest не совпадает с пересчитанным каталогом "
         "(sha256 канонического JSON: sort_keys, separators (',', ':'), ensure_ascii=False)")
    need(inp.execution_mode in ExecutionMode.values, f"execution_mode {inp.execution_mode!r} не из SHADOW|LIVE (C1)")
    need(not inp.conversation_ref or {"conversation_id", "trace_id"} <= set(inp.conversation_ref),
         "conversation_ref — {conversation_id, trace_id} либо пусто")
    if inp.result_status in NO_NBA_STATUSES:
        need(inp.primary is None and not inp.alternatives,
             f"result_status {inp.result_status} не является NBA (§32) — primary и alternatives не пишутся")
    else:
        need(inp.primary is not None, f"result_status {inp.result_status} — NBA-исход без primary (§32)")
        need(len(inp.alternatives) <= 2, f"alternatives: {len(inp.alternatives)} > 2 (Killer PRD §5.1; контракт §10)")


def _check_record(inp: RecommendationInput, label: str, expected_role: str) -> None:
    need = _need(label)
    need(inp.role == expected_role, f"role должен быть {expected_role}")
    need(bool(inp.direction_code.strip()), "direction_code пуст — decision_subject обязателен (B2)")
    need(inp.family in Recommendation.Family.values,
         f"family {inp.family!r} не из ADDRESS/SUPPORT/RECOVER/OBSERVE (B9)")
    need(isinstance(inp.target_outcomes, list), "target_outcomes — список")
    _check_grounds(need, inp.reason_codes, inp.evidence_refs, inp.explanation)
    for name in ("memory_snapshot_ref", "execution_mapping_snapshot_ref", "transaction_snapshot_ref"):
        ref = getattr(inp, name)
        # Ровно три ключа ссылки, не подмножество (DRF-1909): лишний ключ рядом со
        # ссылкой — путь пронести в «оставляемую» при удалении ссылку id человека.
        need(ref is None or (isinstance(ref, dict) and set(ref) == _SNAPSHOT_KEYS),
             f"{name} — ссылка на снимок ровно {sorted(_SNAPSHOT_KEYS)}, не копия и без лишних ключей (B10)")
    if inp.role == Recommendation.Role.ALTERNATIVE:
        need(inp.rerank_reason in Recommendation.RerankReason.values,
             "alternative без rerank_reason (канон v1.1 §10.2)")
    else:
        need(inp.rerank_reason == "" and inp.parent_id is None, "у primary нет parent/rerank_reason")


def _build(inp: RecommendationInput, rset: RecommendationSet, now: datetime,
           parent: Recommendation | None, pk: UUID | None = None) -> Recommendation:
    return Recommendation(
        id=pk or uuid.uuid4(),
        recommendation_set=rset, role=inp.role, parent=parent, rerank_reason=inp.rerank_reason,
        supersedes_id=inp.supersedes_id,
        direction_code=inp.direction_code, target_outcomes=list(inp.target_outcomes), family=inp.family,
        reason_codes=list(inp.reason_codes), evidence_refs=list(inp.evidence_refs),
        explanation=dict(inp.explanation),
        consent_evaluation_ref=dict(inp.consent_evaluation_ref),
        memory_snapshot_ref=inp.memory_snapshot_ref,
        execution_mapping_snapshot_ref=inp.execution_mapping_snapshot_ref,
        transaction_snapshot_ref=inp.transaction_snapshot_ref,
        record_schema_version=RECORD_SCHEMA_VERSION,
        created_at=now, actionable_until=now + ACTIONABILITY_TTL,
    )


def persist(inp: RecommendationSetInput, *, now: datetime | None = None) -> RecommendationSet:
    """Записать выдачу: набор с исходом + (при NBA) primary и ≤2 alternatives + ``created`` на каждую запись.

    Отказ до первой записи, если набор или любая запись не проходят минимум §5,
    провенанс неполон, или исход и наличие NBA расходятся (§32).

    Порядок записи. Набор immutable и после создания не обновляется, а CHECK на
    его строке требует ``primary_id`` уже при вставке. Поэтому id primary
    выбирается заранее, набор пишется с ним, затем primary с этим id — одна
    транзакция; FK ``primary`` проверяется на COMMIT (DEFERRABLE INITIALLY DEFERRED).
    """
    now = now or timezone.now()
    _check_set(inp)
    if inp.primary is not None:
        _check_record(inp.primary, "primary", Recommendation.Role.PRIMARY)
        for i, alt in enumerate(inp.alternatives, start=1):
            _check_record(alt, f"alternative[{i}]", Recommendation.Role.ALTERNATIVE)

    versions = inp.versions
    with transaction.atomic():
        # Снимок — в той же транзакции, что набор: без набора его не бывает, набора без него — тоже.
        snapshot = ContextSnapshot(
            subject_ref=inp.subject_ref, snapshot_version=inp.context_snapshot.snapshot_version,
            content_digest=inp.context_snapshot.content_digest, content=copy.deepcopy(inp.context_snapshot.content),
            created_at=now,
        )
        snapshot.save()
        primary_id = uuid.uuid4() if inp.primary is not None else None
        rset = RecommendationSet(
            subject_ref=inp.subject_ref, intent_id=inp.intent_id,
            semantic_resolution_ref=inp.semantic_resolution_ref, created_at=now,
            execution_mode=inp.execution_mode, conversation_ref=dict(inp.conversation_ref),
            result_status=inp.result_status, readiness_state=inp.readiness_state,
            reason_codes=list(inp.reason_codes), evidence_refs=list(inp.evidence_refs),
            explanation=dict(inp.explanation), safety_evaluation_ref=dict(inp.safety_evaluation_ref),
            context_snapshot=snapshot, primary_id=primary_id,
            decision_policy_version=versions.decision_policy, taxonomy_version=versions.taxonomy,
            safety_policy_version=versions.safety_policy, catalog_mapping_version=versions.catalog_mapping,
            presentation_policy_version=versions.presentation_policy,
            record_schema_version=RECORD_SCHEMA_VERSION,
        )
        rset.save()
        if inp.primary is not None:
            primary = _build(inp.primary, rset, now, parent=None, pk=primary_id)
            primary.save()
            _event(primary, RecommendationEvent.Kind.CREATED, now, producer="Recommendation")
            for alt in inp.alternatives:
                parent = primary if alt.parent_id is None else Recommendation.objects.get(pk=alt.parent_id)
                rec = _build(alt, rset, now, parent=parent)
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
