"""Типы резолвера — контракт §6 плана как код. Никаких чисел-оценок."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

#: Версия правил резолвера. Меняется при любом изменении семантики R*/маркеров.
RULE_VERSION = "map-salon-services/0.1.0-dry-run"


class Rule(StrEnum):
    R0 = "R0"   #: внешний код у услуги салона — входа сегодня нет
    R1 = "R1"   #: exact pair: (каноническая подкатегория, normalized name) ↔ ровно один канон с кодом
    R2 = "R2"   #: approved synonym: normalized(name) == synonym.normalized, ровно один канон


@dataclass(frozen=True)
class RulesEnabled:
    """Какие правила разрешены к auto. По умолчанию — ни одно (OD-NEW-1/2 открыты)."""

    R0: bool = False
    R1: bool = False
    R2: bool = False

    def is_enabled(self, rule: Rule) -> bool:
        return getattr(self, rule.value)

    @classmethod
    def parse(cls, spec: str | None) -> "RulesEnabled":
        names = {s.strip().upper() for s in (spec or "").split(",") if s.strip()}
        unknown = names - {r.value for r in Rule}
        if unknown:
            raise ValueError(f"неизвестные правила: {', '.join(sorted(unknown))}; допустимы R0, R1, R2")
        return cls(**{r.value: (r.value in names) for r in Rule})


class Decision(StrEnum):
    SKIP_DECIDED = "SKIP_DECIDED"
    AUTO_ELIGIBLE = "AUTO_ELIGIBLE"
    AUTO_NOT_ENABLED = "AUTO_NOT_ENABLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNRESOLVED = "UNRESOLVED"
    BLOCKED = "BLOCKED"


class Reason(StrEnum):
    # SKIP_DECIDED
    ALREADY_VERIFIED = "ALREADY_VERIFIED"
    NOT_RECOMMENDABLE = "NOT_RECOMMENDABLE"
    # AUTO_ELIGIBLE / AUTO_NOT_ENABLED
    R0_EXTERNAL_CODE = "R0_EXTERNAL_CODE"
    R1_EXACT_PAIR = "R1_EXACT_PAIR"
    R2_APPROVED_SYNONYM = "R2_APPROVED_SYNONYM"
    # REVIEW_REQUIRED
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    WEAK_EVIDENCE_NAME_ONLY = "WEAK_EVIDENCE_NAME_ONLY"
    COMPOSITION_MARKER = "COMPOSITION_MARKER"
    HEALTH_MARKER = "HEALTH_MARKER"
    MARKETING_WORDING = "MARKETING_WORDING"
    CANDIDATE_PROVISIONAL = "CANDIDATE_PROVISIONAL"
    SYNONYM_AMBIGUOUS = "SYNONYM_AMBIGUOUS"
    # UNRESOLVED
    NO_MATCH = "NO_MATCH"
    # BLOCKED
    SCHEMA_NOT_READY = "SCHEMA_NOT_READY"
    SEED_MISMATCH = "SEED_MISMATCH"
    TEMPLATE_WITHOUT_CODE = "TEMPLATE_WITHOUT_CODE"


#: Какие причины допустимы у какого решения — словарь приложения B, закрытый.
REASONS_BY_DECISION: dict[Decision, frozenset[Reason]] = {
    Decision.SKIP_DECIDED: frozenset({Reason.ALREADY_VERIFIED, Reason.NOT_RECOMMENDABLE}),
    Decision.AUTO_ELIGIBLE: frozenset({Reason.R0_EXTERNAL_CODE, Reason.R1_EXACT_PAIR, Reason.R2_APPROVED_SYNONYM}),
    Decision.AUTO_NOT_ENABLED: frozenset({Reason.R0_EXTERNAL_CODE, Reason.R1_EXACT_PAIR, Reason.R2_APPROVED_SYNONYM}),
    Decision.REVIEW_REQUIRED: frozenset({
        Reason.MULTIPLE_CANDIDATES, Reason.WEAK_EVIDENCE_NAME_ONLY, Reason.COMPOSITION_MARKER,
        Reason.HEALTH_MARKER, Reason.MARKETING_WORDING, Reason.CANDIDATE_PROVISIONAL, Reason.SYNONYM_AMBIGUOUS,
    }),
    Decision.UNRESOLVED: frozenset({Reason.NO_MATCH}),
    Decision.BLOCKED: frozenset({Reason.SCHEMA_NOT_READY, Reason.SEED_MISMATCH, Reason.TEMPLATE_WITHOUT_CODE}),
}


class EvidenceType(StrEnum):
    EXACT_CODE = "EXACT_CODE"            #: R0
    APPROVED_ALIAS = "APPROVED_ALIAS"    #: R2
    EXACT_PAIR_RULE = "EXACT_PAIR_RULE"  #: R1
    NAME_ONLY = "NAME_ONLY"              #: информативно — имя совпало в другой подкатегории; не auto


@dataclass(frozen=True)
class Markers:
    composition: tuple[str, ...] = ()
    health: tuple[str, ...] = ()
    marketing: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedEvidence:
    raw_name: str
    norm_name: str
    raw_category: str
    norm_category: str
    markers: Markers


@dataclass(frozen=True)
class Candidate:
    template_id: UUID
    canonical_code: str | None
    name: str
    category_name: str
    evidence_type: EvidenceType
    lifecycle: str
    requires_health_check: bool
    rule: Rule | None            #: правило, по которому кандидат был бы auto; None — информативный

    @property
    def is_auto_candidate(self) -> bool:
        return self.rule is not None


@dataclass(frozen=True)
class Before:
    template_id: UUID | None
    mapping_status: str


@dataclass(frozen=True)
class ProvenancePlan:
    """Что записал бы apply (MAP-AUTO-06). Здесь — только план, не запись."""

    confirmed_by: None
    confirmed_rule: str
    rule_version: str
    source_ref: str


@dataclass(frozen=True)
class SafetyHandoff:
    """Safety здесь не решается — переносится дальше как есть; ``unknown`` остаётся ``unknown``."""

    raw_name: str
    canonical_rhc: bool | str     #: True | False | "unknown" (нет единственного канона)
    local_rhc: bool | None        #: SalonService.requires_health_check (tri-state)
    health_markers: tuple[str, ...]
    contraindications_ref: str    #: template_id канона, у которого есть contraindications, либо ""
    is_composite: bool
    components: tuple[str, ...]


@dataclass(frozen=True)
class ResolverRow:
    salon_service_id: UUID
    raw_name: str
    decision: Decision
    reason: Reason
    flags: tuple[str, ...]
    rule_id: str | None
    rule_version: str
    seed_version: str
    candidates: tuple[Candidate, ...]
    before: Before
    provenance_plan: ProvenancePlan | None
    safety_handoff: SafetyHandoff
    hint: str = ""                 #: например POSSIBLE_CANON_GAP — подсказка, не решение

    def __post_init__(self) -> None:
        if self.reason not in REASONS_BY_DECISION[self.decision]:
            raise ValueError(f"reason {self.reason} недопустим для decision {self.decision}")
        if self.decision is Decision.AUTO_ELIGIBLE:
            auto = [c for c in self.candidates if c.is_auto_candidate]
            if len(auto) != 1 or self.provenance_plan is None:
                raise ValueError("AUTO_ELIGIBLE только при ровно одном auto-кандидате и плане провенанса")
        elif self.provenance_plan is not None:
            raise ValueError("provenance_plan есть только у AUTO_ELIGIBLE")


@dataclass
class Summary:
    """Счётчики сводки считаются тем же кодом, что и строки («гейту нужен счётчик»)."""

    by_decision: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    total: int = 0

    @classmethod
    def of(cls, rows: list[ResolverRow]) -> "Summary":
        s = cls(total=len(rows))
        for r in rows:
            s.by_decision[r.decision.value] = s.by_decision.get(r.decision.value, 0) + 1
            key = f"{r.decision.value}/{r.reason.value}"
            s.by_reason[key] = s.by_reason.get(key, 0) + 1
        assert sum(s.by_decision.values()) == s.total
        return s
