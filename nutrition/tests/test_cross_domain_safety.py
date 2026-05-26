"""Tests for the cross-domain safety contract (task #91).

Pins each contract clause (§1–§6 in docs/safety/cross_domain_safety_contract.md)
individually plus a snapshot audit against the 5 seed rules. The 3/5
currently-failing seed rules are EXPECTED failures asserted here so the
audit becomes a regression contract: any future curator edit that
silently violates the contract is caught by the test, and any future
fix to a seed string flips the matching assertion (with the test author
updating this file to match).
"""
from __future__ import annotations

import pytest

from nutrition.data.cross_domain_rules_seed import TRACK_E_RULES
from nutrition.services.cross_domain_safety import (
    MANDATORY_EXCLUSIONS,
    has_errors,
    validate_safety_contract,
)


class _RuleStub:
    """Lightweight duck-type matching CrossDomainRule's user-facing fields.

    Avoids hitting the DB for pure-contract tests. The validator only
    reads five attributes, so a dataclass is plenty."""

    def __init__(
        self, *,
        insight: str = "Витамин D ниже нормы — это часто отражается на коже",
        rationale: str = "Массаж с аргановым маслом помогает коже",
        disclaimer: str = "Это не медицинская рекомендация.",
        excluded: list[str] | None = None,
    ):
        self.insight_text_template = insight
        self.rationale_text = rationale
        self.disclaimer_text = disclaimer
        self.excluded_health_flags = (
            excluded if excluded is not None
            else list(MANDATORY_EXCLUSIONS)
        )


# ---------------------------------------------------------------------------
# §1 — Required fields are non-empty
# ---------------------------------------------------------------------------


class TestRequiredFieldsNonEmpty:
    def test_empty_insight_blocks(self):
        v = validate_safety_contract(_RuleStub(insight=""))
        assert any(x.code == "EMPTY" and x.field == "insight_text_template" for x in v)

    def test_whitespace_only_insight_blocks(self):
        v = validate_safety_contract(_RuleStub(insight="   \t  "))
        assert any(x.code == "EMPTY" and x.field == "insight_text_template" for x in v)

    def test_empty_rationale_blocks(self):
        v = validate_safety_contract(_RuleStub(rationale=""))
        assert any(x.code == "EMPTY" and x.field == "rationale_text" for x in v)

    def test_empty_disclaimer_blocks(self):
        v = validate_safety_contract(_RuleStub(disclaimer=""))
        assert any(x.code == "EMPTY" and x.field == "disclaimer_text" for x in v)


# ---------------------------------------------------------------------------
# §2 — No medical-diagnosis terms
# ---------------------------------------------------------------------------


class TestNoMedicalTerms:
    def test_anaemia_in_insight_blocks(self):
        v = validate_safety_contract(
            _RuleStub(insight="Низкое железо — признак анемии"),
        )
        assert any(x.code == "MEDICAL_TERM" for x in v)

    def test_diagnosis_word_blocks(self):
        v = validate_safety_contract(
            _RuleStub(rationale="Это снимает симптом усталости"),
        )
        # "симптом" is medical
        assert any(x.code == "MEDICAL_TERM" for x in v)

    def test_treatment_word_word_boundary(self):
        """'лечит' on its own is medical; ensure word-boundary still fires."""
        v = validate_safety_contract(
            _RuleStub(rationale="Эта процедура лечит кожу"),
        )
        assert any(x.code == "MEDICAL_TERM" for x in v)

    def test_treatment_word_does_not_falsely_match_innocent_words(self):
        """'увлечение' contains 'лечение' as substring — must NOT trigger."""
        v = validate_safety_contract(
            _RuleStub(rationale="Это увлечение многих наших клиентов"),
        )
        assert not any(x.code == "MEDICAL_TERM" for x in v)

    def test_naming_a_nutrient_is_allowed(self):
        """Naming витамин D / железо / омега-3 is contractually fine."""
        v = validate_safety_contract(_RuleStub(
            insight="Витамин D ниже нормы — это часто отражается на коже",
        ))
        assert not any(x.code == "MEDICAL_TERM" for x in v)


# ---------------------------------------------------------------------------
# §3 — No shame-adjacent comparatives
# ---------------------------------------------------------------------------


class TestNoShameLanguage:
    def test_tuskleye_blocks(self):
        v = validate_safety_contract(_RuleStub(
            insight="Железо ниже нормы — кожа выглядит тусклее",
        ))
        assert any(x.code == "SHAME_LANGUAGE" for x in v)

    def test_teryaet_blocks(self):
        v = validate_safety_contract(_RuleStub(
            insight="Кожа теряет упругость быстрее",
        ))
        assert any(x.code == "SHAME_LANGUAGE" for x in v)

    def test_neutral_correlation_passes(self):
        v = validate_safety_contract(_RuleStub(
            insight="Кальций ниже нормы — это часто связано с отёчностью",
        ))
        assert not any(x.code == "SHAME_LANGUAGE" for x in v)


# ---------------------------------------------------------------------------
# §4 — No causation claims
# ---------------------------------------------------------------------------


class TestNoCausationClaims:
    def test_iz_za_blocks(self):
        v = validate_safety_contract(_RuleStub(
            insight="Из-за низкого витамина D кожа сохнет",
        ))
        assert any(x.code == "CAUSATION_CLAIM" for x in v)

    def test_vyzyvaet_blocks(self):
        v = validate_safety_contract(_RuleStub(
            rationale="Дефицит вызывает усталость",
        ))
        # 'дефицит' is fine; 'вызывает' is the violation.
        codes = {x.code for x in v}
        assert "CAUSATION_CLAIM" in codes

    def test_correlation_passes(self):
        v = validate_safety_contract(_RuleStub(
            insight="Витамин D ниже нормы — часто отражается на коже",
        ))
        assert not any(x.code == "CAUSATION_CLAIM" for x in v)


# ---------------------------------------------------------------------------
# §5 — Disclaimer marker required
# ---------------------------------------------------------------------------


class TestDisclaimerMarker:
    def test_missing_marker_blocks(self):
        v = validate_safety_contract(_RuleStub(
            disclaimer="Это рекомендация общего плана.",
        ))
        assert any(x.code == "DISCLAIMER_MARKER_MISSING" for x in v)

    def test_ne_medicin_passes(self):
        v = validate_safety_contract(_RuleStub(
            disclaimer="Это не медицинская рекомендация.",
        ))
        assert not any(x.code == "DISCLAIMER_MARKER_MISSING" for x in v)

    def test_ne_zamenyaet_stem_passes(self):
        """Stem 'не замен' should match 'не заменяет' / 'не замена'."""
        v = validate_safety_contract(_RuleStub(
            disclaimer="Это поддержка, не замена питания.",
        ))
        assert not any(x.code == "DISCLAIMER_MARKER_MISSING" for x in v)

    def test_obratites_k_vrachu_passes(self):
        v = validate_safety_contract(_RuleStub(
            disclaimer="При стойких симптомах обратитесь к врачу.",
        ))
        # NB: "симптом" still flags MEDICAL_TERM — but the marker
        # check (which is what this test pins) is independent.
        assert not any(x.code == "DISCLAIMER_MARKER_MISSING" for x in v)


# ---------------------------------------------------------------------------
# §6 — Mandatory excluded_health_flags
# ---------------------------------------------------------------------------


class TestMandatoryExclusions:
    def test_missing_both_blocks(self):
        v = validate_safety_contract(_RuleStub(excluded=[]))
        assert any(x.code == "MISSING_MANDATORY_EXCLUSION" for x in v)

    def test_missing_only_pregnant_blocks(self):
        v = validate_safety_contract(_RuleStub(excluded=["eating_disorder"]))
        assert any(x.code == "MISSING_MANDATORY_EXCLUSION" for x in v)

    def test_missing_only_eating_disorder_blocks(self):
        v = validate_safety_contract(_RuleStub(excluded=["pregnant"]))
        assert any(x.code == "MISSING_MANDATORY_EXCLUSION" for x in v)

    def test_extra_exclusions_allowed(self):
        """Per-rule additions on top of the mandatory floor are fine."""
        v = validate_safety_contract(_RuleStub(
            excluded=["pregnant", "eating_disorder", "diabetes"],
        ))
        assert not any(x.code == "MISSING_MANDATORY_EXCLUSION" for x in v)

    def test_none_value_treated_as_empty(self):
        """Defensive — JSONField default is [] but legacy rows might
        have None on the field."""
        rule = _RuleStub()
        rule.excluded_health_flags = None
        v = validate_safety_contract(rule)
        assert any(x.code == "MISSING_MANDATORY_EXCLUSION" for x in v)


# ---------------------------------------------------------------------------
# Integration: clean rule passes
# ---------------------------------------------------------------------------


class TestCleanRulePasses:
    def test_minimal_clean_rule(self):
        """Defaults of _RuleStub form a contract-passing rule."""
        v = validate_safety_contract(_RuleStub())
        assert v == [], f"Expected zero violations, got: {v}"

    def test_has_errors_helper(self):
        v = validate_safety_contract(_RuleStub(insight=""))
        assert has_errors(v) is True

    def test_has_errors_helper_when_empty(self):
        assert has_errors([]) is False


# ---------------------------------------------------------------------------
# Audit snapshot — pin the contract verdict on each of the 5 seed rules
# ---------------------------------------------------------------------------


class TestSeedRulesAuditSnapshot:
    """Regression contract for the existing 5 seed rules.

    The §7 audit table in the safety contract doc lists the verdict
    for each seed. These assertions encode that verdict. When legal
    counsel rewrites a string and a seed row starts to pass, the
    matching `expected_pass` flag flips here AND the audit table in
    the doc updates in the same PR — drift between code and doc is
    caught by reviewer eyeball.
    """

    # rule_id → True if the rule is expected to currently PASS the
    # contract, False if it is expected to fail (so we lock in the
    # failure as today's audit baseline).
    EXPECTED = {
        "vitamin_d_deficit_to_argan_massage": False,  # missing pregnant
        "iron_deficit_to_face_glow": False,            # "тусклее" + "анемии"
        "omega3_deficit_to_skin_hydration": False,     # "теряет" + "симптом" + missing pregnant
        "calcium_deficit_to_lymph_drainage": True,
        "b12_deficit_to_relaxation_spa": True,
    }

    @pytest.mark.parametrize(
        "seed",
        TRACK_E_RULES,
        ids=lambda r: r["rule_id"],
    )
    def test_seed_audit(self, seed):
        rule = _RuleStub(
            insight=seed["insight_text_template"],
            rationale=seed["rationale_text"],
            disclaimer=seed["disclaimer_text"],
            excluded=list(seed["excluded_health_flags"]),
        )
        violations = validate_safety_contract(rule)
        passed = not has_errors(violations)
        expected = self.EXPECTED[seed["rule_id"]]

        if expected:
            assert passed, (
                f"Seed rule {seed['rule_id']} was expected to PASS the "
                f"safety contract but produced violations: "
                f"{[(v.field, v.code) for v in violations]}. "
                "If a curator just fixed the strings, congrats — flip "
                "EXPECTED[...] to True in the same PR."
            )
        else:
            assert not passed, (
                f"Seed rule {seed['rule_id']} was expected to FAIL the "
                "safety contract (audit baseline 2026-05-26) but now "
                "passes. If a curator fixed it, flip EXPECTED[...] to "
                "True and update §7 audit table in "
                "docs/safety/cross_domain_safety_contract.md."
            )


# ---------------------------------------------------------------------------
# Model.clean integration smoke
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCrossDomainRuleCleanIntegration:
    """``CrossDomainRule.clean`` should raise ValidationError for an
    ERROR-severity contract violation and return cleanly otherwise.

    Full admin form tests live in their own module; this is just the
    boundary smoke check that the contract is wired in."""

    def _build_rule(self, **overrides):
        from nutrition.models import CrossDomainRule
        kwargs = dict(
            rule_id="test-clean-rule",
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-test",
            insight_text_template=(
                "Витамин D ниже нормы — часто отражается на коже"
            ),
            rationale_text="Массаж с аргановым маслом помогает коже",
            disclaimer_text="Это не медицинская рекомендация.",
            excluded_health_flags=list(MANDATORY_EXCLUSIONS),
        )
        kwargs.update(overrides)
        return CrossDomainRule(**kwargs)

    def test_clean_passes_on_valid_rule(self):
        rule = self._build_rule()
        rule.clean()  # no exception

    def test_clean_raises_on_medical_term(self):
        from django.core.exceptions import ValidationError
        rule = self._build_rule(
            insight_text_template="Низкое железо — признак анемии",
        )
        with pytest.raises(ValidationError) as exc:
            rule.clean()
        assert "insight_text_template" in exc.value.message_dict

    def test_clean_raises_on_missing_mandatory_exclusion(self):
        from django.core.exceptions import ValidationError
        rule = self._build_rule(excluded_health_flags=["eating_disorder"])
        with pytest.raises(ValidationError) as exc:
            rule.clean()
        assert "excluded_health_flags" in exc.value.message_dict
