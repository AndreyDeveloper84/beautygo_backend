# Cross-Domain Safety Contract

> **Task #91 (Tau post-pilot handoff, advanced to PRE_PILOT 2026-05-26).**
> Source: ai-bot-platform investigation 2026-05-25.
> Implementation: `nutrition/services/cross_domain_safety.py` +
> `CrossDomainRule.clean()` gate.

## TL;DR

When a `CrossDomainRule` recommends a beauty service in response to a
nutrition signal, the user reads three strings:
`insight_text_template`, `rationale_text`, `disclaimer_text`. Tau's
investigation found that the existing seed rules ship with medical-
diagnosis terms, shame-adjacent comparatives, causation language, and
incomplete health-flag exclusions — issues the English-language seed
docstring failed to prevent because legal counsel is non-technical.

This document defines a programmatic contract enforced at model-save
time. Rules failing any **ERROR**-severity check cannot be saved.

## Background — what Tau found

The bot-platform team removed the cross-domain card from MVP because:

1. **Anti-medical / anti-shame filter MISSING** — no programmatic
   guard between curator and production.
2. **`rule_slug` examples include medical templates** — e.g. the
   `vitamin_d_deficit_to_argan_massage` seed treats a clinical
   deficit signal as the trigger.
3. **Disclaimer field exists** but cross-stream rendering (bot card
   vs. mobile UI) was never specced, so the disclaimer might be
   dropped, truncated, or visually de-emphasised by the surface.

## The contract

A rule passes iff ALL of:

### §1 — Required fields are non-empty

`insight_text_template`, `rationale_text`, `disclaimer_text` must all
be non-blank. Empty string or whitespace-only fails.

### §2 — No medical-diagnosis terms in user-facing strings

The three user-facing strings must not contain words that name a
clinical condition or diagnosis:

> "анемия", "диагноз", "болезнь", "недостаточност", "патолог",
> "синдром", "иммунодефицит", "гипотиреоз", "гипертиреоз",
> "лекарств", "терапи", "профилакти", "симптом",
> + word-boundary "лечение" / "лечит" / "лечат".

Naming a nutrient ("витамин D", "железо", "омега-3") is FINE — that
is what the user sees on a Food Scanner card already. Naming the
deficit-state ("низкий витамин D 7+ дней") is FINE — that is the
factual observation. Naming the *clinical diagnosis* it implies
("анемия", "гипотиреоз") is FORBIDDEN — we do not have the licence
to make that claim.

### §3 — No shame-adjacent comparatives

The three user-facing strings must not contain comparative
degradation language about the user's body or appearance:

> "хуже", "тусклее", "тускл", "слабее", "слаб", "плохо выглядит",
> "выглядит плохо", "теряет", "ухудшает", "стареет", "увядает".

The user is not failing at being a person; the insight is a soft
nudge, not a critique. "Кожа выглядит тусклее" reads as an
indictment of the reader; "кожа реагирует на изменения в питании"
reads as observation. Conservative tuning — borderline cases
(non-comparative usage in benign contexts) get blocked too. Legal
counsel may approve a waiver by reformulating the string.

### §4 — No causation claims

The three user-facing strings must not state a causal link between
the nutrition signal and the cosmetic state:

> "из-за", "вызывает", "вызыва", "приводит к", "становится причиной",
> "провоцирует".

We show a **correlation** ("часто отражается", "может ощущаться") —
that line matters under Закон «О рекламе» ст. 24 (рекламы
медицинских услуг). Causation framing requires medical licence we
do not have.

### §5 — Disclaimer carries the non-medical marker

`disclaimer_text` must contain one of:

> "не медицин", "не лечен", "не замен", "обсуди с врач",
> "обратитесь к врач".

These are stems — different declensions of the same idea (e.g. "не
заменяет", "не замена", "не заменит") all reduce to "не замен" and
match.

The marker tells the user, in plain language, that the recommendation
is not a substitute for medical care. Strict checking is on purpose:
a marginal disclaimer that only implies the disclaimer is the
canonical failure mode under Закон «О рекламе».

### §6 — Mandatory health-flag exclusions

`excluded_health_flags` must contain BOTH `pregnant` AND
`eating_disorder`. Cross-domain nudges are forbidden for both
populations regardless of the rule's framing — the cosmetic claim
does not relieve us of the duty to refuse to nudge a vulnerable user.

Per-rule additions (e.g. `excluded_health_flags: ["pregnant",
"eating_disorder", "diabetes"]`) are allowed; we are only enforcing
the floor.

### Activation gates (existing, unchanged)

These are model-level flags, not contract checks. A rule is consumed
by the engine only when BOTH are true:

- `legal_reviewed=True` — set by curator/counsel after sign-off.
- `is_active=True` — set by PO/admin after activation review.

Both default `False`, so a rule lands inert.

## Audit — existing 5 seed rules

Ran `validate_safety_contract` against
`nutrition/data/cross_domain_rules_seed.py` (2026-05-26). The seed
file commits unchanged — fixes are owned by legal counsel, not this
PR. Activation is blocked by the new `clean()` gate until each
violation is addressed.

| Rule | §1 | §2 | §3 | §4 | §5 | §6 | Verdict |
|---|---|---|---|---|---|---|---|
| `vitamin_d_deficit_to_argan_massage` | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ missing `pregnant` | **FAIL** |
| `iron_deficit_to_face_glow` | ✓ | ✗ "анемии" | ✗ "тусклее" | ✓ | ✓ | ✓ | **FAIL** |
| `omega3_deficit_to_skin_hydration` | ✓ | ✗ "симптом" | ✗ "теряет" | ✓ | ✓ (matches "не замен") | ✗ missing `pregnant` | **FAIL** |
| `calcium_deficit_to_lymph_drainage` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **PASS** |
| `b12_deficit_to_relaxation_spa` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **PASS** |

3/5 rules need curator attention before activation. The contract
locks them out automatically — `is_active=True` triggers
`clean()` and `ValidationError` until the strings are fixed.

## §7 — Cross-stream rendering spec (deliverable #4)

The `CrossDomainRecommendation` DTO (`cross_domain_engine.py:79`)
carries six user-facing fields:

```
shown_id, rule_id, nutrition_trigger, service_category_slug,
insight_text, rationale_text, disclaimer_text,
data_points, severity
```

### Render contract — Bot card (Telegram, MAX)

1. **Title block** — render `insight_text` verbatim. Maximum one
   line on small viewports; do not auto-paraphrase.
2. **Rationale block** — render `rationale_text` directly under the
   insight. Same font weight; this is not a footnote.
3. **Disclaimer block** — render `disclaimer_text` as the **last
   visible** block of the card, ABOVE the action buttons. Required
   visual contract:
   - Inline (NOT collapsed / NOT behind a tap).
   - Font size ≥ rationale font size (no de-emphasis).
   - Italic permitted; greyed-out or sub-text size NOT permitted.
4. **Action buttons** — Book / Dismiss / "Откуда ты знаешь?". The
   "Dismiss" affordance MUST be available; users cannot be funnelled
   into Book-only.
5. **Card footer** — never embed the rule_slug or trigger slug. Those
   are internal; users see the natural-language strings only.

### Render contract — Mobile UI (Ayla client app, Phase 5+)

Phase 5+ scope; recorded here so the contract is set when the surface
is built. Same five points as Bot card, plus:

6. **Disclaimer must be screen-readable** — `accessibilityLabel`
   reads the full disclaimer to assistive tech users; not a separate
   tap.
7. **Disclaimer persists** when the card is shared / screenshotted —
   meaning the disclaimer block is part of the share-render, not
   only the live view.

### Render contract — Web (any future surface)

Same contract. Disclaimer above the fold of the card, never collapsed
behind "Read more".

## §8 — Error code: SCAN_NOT_FOUND

Status: **ALREADY IMPLEMENTED** (verified 2026-05-26). Lives in
`core/errors.py:144`, used by `nutrition/views.py:333`, pinned by
`nutrition/tests/test_food_log.py:231`. No change needed for this
task.

## What this contract does NOT cover

- **Vendor accuracy** of the underlying nutrition trigger detector.
  If `detect_patterns` misfires (false-low vitamin D), the contract
  cannot save us — it only governs the user-facing strings.
- **Medical appropriateness** of the recommended service. By design,
  we never make a medical claim about the service; the routing is
  cosmetic-to-cosmetic and the disclaimer disowns clinical effect.
- **Frequency** of recommendations across the user's journey. The
  cross-domain engine has its own cooldown ladder (30/14/7d + global
  cap + double-skip pause) and a global eating-disorder gate.
- **Tenant boundary** — a rule is global to the platform, not per-
  tenant; supply-side checks (`_has_supply` in the engine) handle
  routing per-tenant.

## Roll-forward

When a rule fails the contract, the only sanctioned path is:

1. Curator (legal counsel) rewrites the offending string.
2. PR review confirms the new string passes
   `validate_safety_contract` (unit test or admin save dry-run).
3. `legal_reviewed=True` flipped only after both lawyer sign-off
   AND the contract validator returns zero ERROR violations.
4. `is_active=True` flipped only after the above plus supply audit
   (`python manage.py audit_cross_domain_supply` returns OK for the
   rule's service_category_slug).

## Test coverage

`nutrition/tests/test_cross_domain_safety.py` pins each contract
clause individually plus a snapshot test against the 5 seed rules
documented in the audit table above. The 3/5 currently-failing seeds
are expected failures asserted by the test — making the audit a
regression contract, not a one-off.

## References

- `nutrition/services/cross_domain_safety.py` — validator module.
- `nutrition/models.py` `CrossDomainRule.clean` — model gate.
- `nutrition/data/cross_domain_rules_seed.py` — current 5 rules.
- `nutrition/services/cross_domain_engine.py` — engine that consumes
  validated rules.
- `core/errors.py` `ErrorCode.SCAN_NOT_FOUND` — deliverable #3.
- ai-bot-platform task #91 — origin.
- Закон «О рекламе» ст. 24 — regulatory source.

_Last updated: 2026-05-26._
