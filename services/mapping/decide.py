"""Стадии 4–6 — детерминированное решение, review payload, план провенанса.

Порядок ветвей — §6 плана, и он фиксирован: pre-decided → маркеры →
ровно один auto-кандидат → несколько → только информативные → ничего.
Каждая строка получает ровно один ``decision`` и ровно один ``reason``.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from services.mapping.normalize import split_components
from services.mapping.types import (
    RULE_VERSION,
    Before,
    Candidate,
    Decision,
    NormalizedEvidence,
    ProvenancePlan,
    Reason,
    ResolverRow,
    Rule,
    RulesEnabled,
    SafetyHandoff,
)

#: Значения `SalonService.mapping_status`, которые человек уже решил.
#: Дословно, а не через импорт модели: пакет обязан оставаться без
#: ORM-зависимостей на стадии решения (чистые функции, план §6).
VERIFIED = "verified"
NOT_RECOMMENDABLE = "not_recommendable"
APPROVED = "approved"

_RULE_REASON = {
    Rule.R0: Reason.R0_EXTERNAL_CODE,
    Rule.R1: Reason.R1_EXACT_PAIR,
    Rule.R2: Reason.R2_APPROVED_SYNONYM,
}


@dataclass(frozen=True)
class RowInput:
    salon_service_id: UUID
    mapping_status: str
    template_id: UUID | None
    local_rhc: bool | None
    evidence: NormalizedEvidence
    candidates: tuple[Candidate, ...]
    discovery_notes: tuple[str, ...]


def _provenance(rule: Rule, seed_version: str, report_ref: str, before: Before) -> ProvenancePlan:
    return ProvenancePlan(
        confirmed_by=None,
        confirmed_rule=f"map_salon_services:{rule.value}",
        rule_version=RULE_VERSION,
        source_ref=(
            f"auto:{rule.value} seed={seed_version} report={report_ref} "
            f"prev_template={before.template_id or 'none'}"
        ),
    )


def _safety(inp: RowInput, single: Candidate | None) -> SafetyHandoff:
    m = inp.evidence.markers
    return SafetyHandoff(
        raw_name=inp.evidence.raw_name,
        canonical_rhc=single.requires_health_check if single is not None else "unknown",
        local_rhc=inp.local_rhc,
        health_markers=m.health,
        contraindications_ref=str(single.template_id) if single is not None else "",
        is_composite=bool(m.composition),
        components=split_components(inp.evidence.raw_name) if m.composition else (),
    )


def decide(inp: RowInput, rules: RulesEnabled, *, seed_version: str, report_ref: str) -> ResolverRow:
    ev = inp.evidence
    before = Before(template_id=inp.template_id, mapping_status=inp.mapping_status)
    auto = [c for c in inp.candidates if c.is_auto_candidate]
    single = auto[0] if len(auto) == 1 else None
    flags = tuple(
        [f"composition:{x}" for x in ev.markers.composition]
        + [f"health:{x}" for x in ev.markers.health]
        + [f"marketing:{x}" for x in ev.markers.marketing]
        + list(inp.discovery_notes)
    )

    def row(decision, reason, *, rule=None, plan=None, hint=""):
        return ResolverRow(
            salon_service_id=inp.salon_service_id, raw_name=ev.raw_name, decision=decision, reason=reason,
            flags=flags, rule_id=rule.value if rule else None, rule_version=RULE_VERSION, seed_version=seed_version,
            candidates=inp.candidates, before=before, provenance_plan=plan,
            safety_handoff=_safety(inp, single), hint=hint,
        )

    # 1. Решено человеком — терминально.
    if inp.mapping_status == VERIFIED:
        return row(Decision.SKIP_DECIDED, Reason.ALREADY_VERIFIED)
    if inp.mapping_status == NOT_RECOMMENDABLE:
        return row(Decision.SKIP_DECIDED, Reason.NOT_RECOMMENDABLE)

    # 2. Маркеры блокируют auto — до любого подсчёта кандидатов.
    if ev.markers.composition:
        return row(Decision.REVIEW_REQUIRED, Reason.COMPOSITION_MARKER)
    if ev.markers.health:
        return row(Decision.REVIEW_REQUIRED, Reason.HEALTH_MARKER)
    if ev.markers.marketing:
        return row(Decision.REVIEW_REQUIRED, Reason.MARKETING_WORDING)
    if "SYNONYM_AMBIGUOUS" in inp.discovery_notes:
        return row(Decision.REVIEW_REQUIRED, Reason.SYNONYM_AMBIGUOUS)

    # 3. Ровно один auto-кандидат.
    if single is not None:
        rule = single.rule
        if single.canonical_code is None:
            return row(Decision.BLOCKED, Reason.TEMPLATE_WITHOUT_CODE, rule=rule)
        if single.lifecycle != APPROVED:
            return row(Decision.REVIEW_REQUIRED, Reason.CANDIDATE_PROVISIONAL, rule=rule)
        if not rules.is_enabled(rule):
            return row(Decision.AUTO_NOT_ENABLED, _RULE_REASON[rule], rule=rule)
        return row(Decision.AUTO_ELIGIBLE, _RULE_REASON[rule], rule=rule,
                   plan=_provenance(rule, seed_version, report_ref, before))

    # 4. Несколько auto-кандидатов — не выбираем.
    if len(auto) > 1:
        return row(Decision.REVIEW_REQUIRED, Reason.MULTIPLE_CANDIDATES)

    # 5. Только информативные совпадения.
    if inp.candidates:
        return row(Decision.REVIEW_REQUIRED, Reason.WEAK_EVIDENCE_NAME_ONLY)

    # 6. Ничего. CANON_GAP — только слово владельца; здесь лишь подсказка.
    return row(Decision.UNRESOLVED, Reason.NO_MATCH, hint="POSSIBLE_CANON_GAP")
