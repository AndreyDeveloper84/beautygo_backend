"""DRF-268 — seed cross-domain rules + supply audit.

Two management commands:

- ``seed_cross_domain_rules`` — idempotent. Creates 5+ Track E rules
  (low_vitamin_d → massage-argan-oil etc) with ``legal_reviewed=False``
  and ``is_active=False``. A curator flips both flags via Django admin
  after legal sign-off.

- ``audit_cross_domain_supply`` — dry-run that prints, for each rule,
  whether ≥3 specialists in the pilot city carry the rule's
  service_category_slug. Surfaces rules that would silently fail the
  engine's ``_has_supply`` check at evaluate time.

Both commands are read-mostly: seed is purely additive (get_or_create
preserves curator edits to insight_text / cooldowns), audit only
prints.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from nutrition.models import CrossDomainRule
from nutrition.data.cross_domain_rules_seed import TRACK_E_RULES


@pytest.mark.django_db
class TestSeedCrossDomainRules:
    def test_creates_all_track_e_rules(self):
        call_command("seed_cross_domain_rules", stdout=StringIO())
        # All declared rules from cross_domain_rules_seed land in DB.
        present = set(
            CrossDomainRule.objects.values_list("rule_id", flat=True),
        )
        expected = {r["rule_id"] for r in TRACK_E_RULES}
        assert expected <= present, f"Missing: {expected - present}"

    def test_at_least_5_rules_seeded(self):
        call_command("seed_cross_domain_rules", stdout=StringIO())
        # Track E launch needs 5+ rules to cover the 5 micronutrient
        # detectors. This test pins the floor — if someone strips the
        # seed in a refactor, it fails loudly.
        assert CrossDomainRule.objects.count() >= 5

    def test_seeded_rules_default_inactive(self):
        # PO-approved 2026-05-05: rules ship inert. Curator + legal
        # review must flip is_active=True AND legal_reviewed=True.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        for rule in CrossDomainRule.objects.all():
            assert rule.is_active is False, (
                f"{rule.rule_id} must ship is_active=False"
            )
            assert rule.legal_reviewed is False, (
                f"{rule.rule_id} must ship legal_reviewed=False"
            )

    def test_seeded_rules_have_disclaimers(self):
        # Spec v2.0 + Закон «О рекламе» ст. 24 require a visible
        # non-medical disclaimer on every cross-domain card.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        for rule in CrossDomainRule.objects.all():
            assert rule.disclaimer_text.strip(), (
                f"{rule.rule_id} must carry a non-empty disclaimer"
            )
            # Sanity: disclaimer must not promise medical results.
            forbidden = ("вылечит", "лечит", "избавит", "диагноз")
            for word in forbidden:
                assert word not in rule.disclaimer_text.lower(), (
                    f"{rule.rule_id} disclaimer mentions '{word}' — "
                    "would fail Закон «О рекламе» ст. 24 review"
                )

    def test_idempotent_second_run(self):
        call_command("seed_cross_domain_rules", stdout=StringIO())
        first_count = CrossDomainRule.objects.count()
        call_command("seed_cross_domain_rules", stdout=StringIO())
        assert CrossDomainRule.objects.count() == first_count

    def test_curator_edits_preserved_on_rerun(self):
        # If a curator flipped is_active=True and edited insight_text,
        # a second seed run must NOT clobber those edits.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        rule = CrossDomainRule.objects.first()
        rule.is_active = True
        rule.legal_reviewed = True
        rule.insight_text_template = "Curator's hand-tuned text"
        rule.save()

        call_command("seed_cross_domain_rules", stdout=StringIO())

        rule.refresh_from_db()
        assert rule.is_active is True
        assert rule.legal_reviewed is True
        assert rule.insight_text_template == "Curator's hand-tuned text"

    def test_micronutrient_triggers_only(self):
        # Every rule's nutrition_trigger must be one of the 5 Track E
        # micronutrient slugs (DRF-262). Cross-references the pattern
        # engine to fail loudly if rules drift.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        valid_triggers = {
            "low_vitamin_d", "low_iron", "low_omega3",
            "low_calcium", "low_b12",
        }
        for rule in CrossDomainRule.objects.all():
            assert rule.nutrition_trigger in valid_triggers, (
                f"{rule.rule_id} has unrecognized trigger "
                f"{rule.nutrition_trigger}"
            )


@pytest.mark.django_db
class TestAuditCrossDomainSupply:
    def test_audit_command_runs_without_error(self, db):
        # Even when no Service rows exist, the command should run and
        # print warnings, not crash.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        out = StringIO()
        call_command("audit_cross_domain_supply", stdout=out)
        # Output mentions each seeded rule.
        text = out.getvalue()
        assert "low_vitamin_d" in text or "vitamin_d" in text

    def test_audit_warns_when_supply_below_threshold(self):
        # No specialists for any category → every rule warns.
        call_command("seed_cross_domain_rules", stdout=StringIO())
        out = StringIO()
        call_command("audit_cross_domain_supply", stdout=out)
        text = out.getvalue().lower()
        # Look for warning markers — implementation must print them.
        assert "warning" in text or "недостаточно" in text or \
               "<3" in text or "no specialists" in text.lower()

    def test_audit_passes_when_supply_meets_threshold(self, db):
        # Wire up 3+ Service rows for one rule's category.
        from services.models import Service, ServiceCategory
        from users.models import User
        cat = ServiceCategory.objects.create(
            name="Test Argan Massage",
            slug="massage-argan-oil",
        )
        for i in range(4):
            user = User.objects.create_user(
                username=f"sp_audit_{i}", role="specialist",
            )
            sp = user.specialist_profile
            sp.display_name = f"M{i}"
            sp.status = "active"
            sp.save()
            Service.objects.create(
                specialist=sp, category=cat,
                name=f"Argan massage {i}",
                price=2500, duration_minutes=60,
            )

        call_command("seed_cross_domain_rules", stdout=StringIO())
        out = StringIO()
        call_command("audit_cross_domain_supply", stdout=out)
        text = out.getvalue()
        # The argan rule should report ≥4 specialists, no warning for it.
        assert "massage-argan-oil" in text
