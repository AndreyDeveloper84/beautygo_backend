"""Cross-domain safety contract — task #91 (Tau post-pilot handoff).

Programmatic enforcement of the rules in
``docs/safety/cross_domain_safety_contract.md``. The
``CrossDomainRule`` model's ``clean()`` calls into this module so an
admin cannot save (or activate) a rule whose user-facing strings or
exclusion flags violate the contract.

Why programmatic, not just process: Tau's 2026-05-25 investigation
found that the existing seed rules ship with medical-condition
language and shame-adjacent comparatives despite an English-language
docstring telling the curator to avoid them. A linter is the only
guarantee — process drift is the rule, not the exception, when the
curator is non-technical legal counsel.

## Contract summary

A rule passes the contract iff ALL of:

1. ``insight_text_template``, ``rationale_text``, ``disclaimer_text``
   are non-empty.
2. None of those three fields contains forbidden **medical-diagnosis**
   words (e.g. "анемия", "диагноз", "болезнь", "недостаточность").
   Naming a nutrient — "витамин D" — is fine; naming a clinical
   condition is not.
3. None of those three fields contains **shame-adjacent comparatives**
   ("хуже", "тусклее", "слабее", "теряет", "выглядит плохо"). The
   user is not failing at being a person; the insight is a soft
   nudge, not a critique.
4. None of those fields contains **causation** language ("из-за",
   "вызывает", "приводит к"). The user is shown a correlation, not a
   causal claim — that line matters under Закон «О рекламе» ст. 24.
5. ``disclaimer_text`` contains the **non-medical marker**
   ("не медицин" substring or one of the approved phrasings). The
   stricter "обратитесь к врачу" follow-up is required for any
   chronic-deficit framing.
6. ``excluded_health_flags`` includes the **mandatory exclusions**
   ``pregnant`` and ``eating_disorder`` — both are populations for
   whom cross-domain nudges are forbidden regardless of the rule.

Violations are reported with structured ``Violation`` objects so the
admin form can highlight which field failed. ERROR-severity
violations block save; WARNING-severity violations are logged but
allowed (so a marginal call doesn't trap legal counsel in iteration).

## What this does NOT enforce

- The bot/mobile *rendering* of the disclaimer (font size, dismissal
  affordance) — that lives in the surface implementations and is
  specced in the safety doc.
- Vendor accuracy of the nutrition trigger detector — wrong-input is
  caught at the pattern-detection layer.
- Whether the recommended service is medically appropriate — the
  point of the cross-domain contract is that we never make that
  claim; the rule routes a *cosmetic* service against a *cosmetic*
  symptom, with the disclaimer disowning any clinical effect.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


# Mandatory excluded health flags — see contract item 6.
MANDATORY_EXCLUSIONS = ("pregnant", "eating_disorder")


# Forbidden medical-diagnosis terms (Cyrillic + Latin variants where
# the loanword is in common Russian use). Whole-word match so "анемия"
# blocks but "поднимается" does not (despite containing "анемия" in
# the regex-naive case — \b boundaries fix that).
_MEDICAL_TERMS = (
    "анемия", "анемии", "анемичн",
    "диагноз", "диагност",
    "болезнь", "болезни", "болезн",
    "недостаточност",
    "патолог",
    "синдром",
    "иммунодефицит",
    "гипотиреоз",
    "гипертиреоз",
    # NB: "лечение" / "лечит" / "лечат" handled separately via
    # _WORD_BOUNDARY_TERMS so we don't false-match "увлечение",
    # "наплечит", "залечат" via naive substring.
    "лекарств",
    "терапи",
    "профилакти",
    "симптом",
)


# Shame-adjacent comparatives. Tuned conservatively — these words are
# fine in many contexts, but in a *user-facing nudge about their own
# body* they read as critique. The contract errs on the side of
# rejecting; legal counsel can override per-rule via a documented
# waiver (not implemented here — admin can suppress by editing the
# string).
_SHAME_TERMS = (
    "хуже",
    "тусклее", "тускл",
    "слабее", "слаб",
    "плохо выглядит", "выглядит плохо",
    "теряет",
    "ухудшает",
    "стареет",
    "увядает",
)


# Causation language. "Часто отражается", "может ощущаться" — fine
# (observed correlation). "Из-за дефицита кожа стала тусклой" —
# causation, banned.
_CAUSATION_TERMS = (
    "из-за",
    "вызывает", "вызыва",
    "приводит к",
    "становится причиной",
    "провоцирует",
)


# Disclaimer must contain one of these substrings. Case-insensitive.
# Stems chosen so different declensions of the same idea match —
# "не заменяет" / "не замена" / "не заменит" all reduce to "не замен".
_DISCLAIMER_MARKERS = (
    "не медицин",
    "не лечен",
    "не замен",
    "обсуди с врач",
    "обратитесь к врач",
)


VIOLATION_SEVERITY_ERROR = "error"
VIOLATION_SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Violation:
    """A single contract violation.

    ``field`` is the ``CrossDomainRule`` attribute that failed.
    ``code`` is a short slug suitable for analytics / log filters.
    ``severity`` is one of the module constants. ``message`` is a
    Russian sentence the admin form surfaces verbatim.
    """
    field: str
    code: str
    severity: str
    message: str


def _word_in_text(needles: Sequence[str], text: str) -> str | None:
    """Return the first needle that appears in ``text`` (substring,
    case-insensitive), or None. Used for the simple keyword tables —
    whole-word boundaries are not required because the tables are
    deliberately tuned to substrings that don't false-match common
    Russian prefixes (e.g. "анем" excluded; we use stems instead)."""
    lower = text.lower()
    for needle in needles:
        if needle.lower() in lower:
            return needle
    return None


# Word-boundary regex for the few keywords where prefix collision is a
# real risk (e.g. "лечит" vs "залечит"). We keep this set tiny to keep
# the matcher cheap and the violation messages predictable.
_WORD_BOUNDARY_TERMS = {
    "лечит": re.compile(r"\bлечит\w*\b", re.IGNORECASE),
    "лечат": re.compile(r"\bлечат\w*\b", re.IGNORECASE),
    "лечение": re.compile(r"\bлечени\w*\b", re.IGNORECASE),
}


def _wb_word_in_text(text: str) -> str | None:
    for label, pattern in _WORD_BOUNDARY_TERMS.items():
        if pattern.search(text):
            return label
    return None


def _validate_field(
    *, field_name: str, text: str, violations: list[Violation],
) -> None:
    """Apply the per-field checks (anti-medical / anti-shame /
    anti-causation) to a single user-facing string."""
    if not text or not text.strip():
        violations.append(Violation(
            field=field_name,
            code="EMPTY",
            severity=VIOLATION_SEVERITY_ERROR,
            message=f"Поле {field_name} не может быть пустым.",
        ))
        return

    medical = _word_in_text(_MEDICAL_TERMS, text) or _wb_word_in_text(text)
    if medical:
        violations.append(Violation(
            field=field_name,
            code="MEDICAL_TERM",
            severity=VIOLATION_SEVERITY_ERROR,
            message=(
                f"Поле {field_name} содержит медицинский термин "
                f"'{medical}'. Cross-domain insights не должны "
                "ссылаться на диагнозы или клинические состояния — "
                "см. docs/safety/cross_domain_safety_contract.md §2."
            ),
        ))

    shame = _word_in_text(_SHAME_TERMS, text)
    if shame:
        violations.append(Violation(
            field=field_name,
            code="SHAME_LANGUAGE",
            severity=VIOLATION_SEVERITY_ERROR,
            message=(
                f"Поле {field_name} содержит обесценивающее слово "
                f"'{shame}'. Формулировка должна быть нейтральной — "
                "см. anti-shame правило §3."
            ),
        ))

    causation = _word_in_text(_CAUSATION_TERMS, text)
    if causation:
        violations.append(Violation(
            field=field_name,
            code="CAUSATION_CLAIM",
            severity=VIOLATION_SEVERITY_ERROR,
            message=(
                f"Поле {field_name} содержит причинно-следственную "
                f"связку '{causation}'. Cross-domain rule показывает "
                "корреляцию, не причинность — см. §4."
            ),
        ))


def validate_safety_contract(rule) -> list[Violation]:
    """Apply the full cross-domain safety contract to a rule.

    ``rule`` is a ``CrossDomainRule`` instance (or a duck-type with the
    same five fields — keeping the signature loose lets us validate
    serializer input as well as model instances).

    Returns a list of ``Violation`` ordered by field then severity.
    Callers split ERROR vs WARNING:

    - ``CrossDomainRule.clean`` raises ``ValidationError`` if any
      ERROR violation is present.
    - The admin Django form surfaces all violations as inline errors
      so legal counsel can fix every issue in one pass instead of
      iterating one-at-a-time.

    Empty list = rule passes. No side effects (no logging, no DB
    writes) — callers decide what to do.
    """
    violations: list[Violation] = []

    _validate_field(
        field_name="insight_text_template",
        text=rule.insight_text_template or "",
        violations=violations,
    )
    _validate_field(
        field_name="rationale_text",
        text=rule.rationale_text or "",
        violations=violations,
    )
    _validate_field(
        field_name="disclaimer_text",
        text=rule.disclaimer_text or "",
        violations=violations,
    )

    # Disclaimer must contain the non-medical marker. We check this
    # only when the disclaimer is otherwise non-empty (we already
    # raised EMPTY above if it was blank).
    disc = (rule.disclaimer_text or "").lower()
    if disc.strip():
        if not any(marker in disc for marker in _DISCLAIMER_MARKERS):
            violations.append(Violation(
                field="disclaimer_text",
                code="DISCLAIMER_MARKER_MISSING",
                severity=VIOLATION_SEVERITY_ERROR,
                message=(
                    "Disclaimer должен содержать одну из обязательных "
                    "формулировок: 'не медицин', 'не лечен', 'не "
                    "заменяет', 'обсуди с врач', 'обратитесь к врач'. "
                    "Без неё рекомендация читается как медицинская — "
                    "см. §5."
                ),
            ))

    # Mandatory excluded health flags — pregnant + eating_disorder.
    # The model field is JSONField(default=list). Cope with None too.
    excluded = set(rule.excluded_health_flags or [])
    missing = [m for m in MANDATORY_EXCLUSIONS if m not in excluded]
    if missing:
        violations.append(Violation(
            field="excluded_health_flags",
            code="MISSING_MANDATORY_EXCLUSION",
            severity=VIOLATION_SEVERITY_ERROR,
            message=(
                "excluded_health_flags обязательно должен включать "
                f"{', '.join(MANDATORY_EXCLUSIONS)}. Отсутствуют: "
                f"{', '.join(missing)}. См. §6."
            ),
        ))

    return violations


def has_errors(violations: Sequence[Violation]) -> bool:
    """True if any of ``violations`` is severity ERROR."""
    return any(v.severity == VIOLATION_SEVERITY_ERROR for v in violations)
