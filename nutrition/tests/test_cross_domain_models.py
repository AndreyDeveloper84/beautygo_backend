"""DRF-263 — CrossDomainRule + CrossDomainShownRule models.

The cross-domain rule engine bridges Track E pattern detection
(DRF-262) to beauty-service recommendations. Each rule maps a
nutrition trigger (e.g. ``low_vitamin_d``) to a service category
(e.g. ``massage-argan-oil``) with a legal-reviewed insight + rationale.

Two models:

- ``CrossDomainRule`` — the catalogue. ``is_active=False`` and
  ``legal_reviewed=False`` by default; a rule is only evaluated when
  both flip to True. Cooldown windows + skip-extension are stored
  per-rule so we can tune.

- ``CrossDomainShownRule`` — history of recommendations served to a
  user. Carries ``shown_at`` (engine GET), ``seen_at`` (surface POSTs
  /seen/), and ``user_action`` (none|dismissed|paused_7d|converted).
  Cooldown logic reads these.

Auto-confirm-5min: the engine treats a row as "seen" if either
``seen_at`` is non-null OR ``shown_at + 5 minutes`` has passed.
Defends against client crashes between GET and the explicit /seen/ POST.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from nutrition.models import CrossDomainRule, CrossDomainShownRule


User = get_user_model()


@pytest.fixture
def vitamin_d_rule(db):
    return CrossDomainRule.objects.create(
        rule_id="vitamin_d_deficit_to_argan_massage",
        nutrition_trigger="low_vitamin_d",
        service_category_slug="massage-argan-oil",
        service_modifier="",
        insight_text_template=(
            "За последние {N} дней витамин D ниже нормы — "
            "это часто отражается на коже"
        ),
        rationale_text=(
            "Массаж с аргановым маслом помогает коже получить "
            "витамин E и F"
        ),
        disclaimer_text=(
            "Это не медицинская рекомендация. При стабильном "
            "дефиците обратитесь к врачу"
        ),
    )


@pytest.fixture
def cross_domain_user(db):
    return User.objects.create_user(
        username="cd_test", password="x",
        role="client", phone="+79990700001",
    )


@pytest.mark.django_db
class TestCrossDomainRuleModel:
    def test_default_is_active_false(self, vitamin_d_rule):
        assert vitamin_d_rule.is_active is False
        assert vitamin_d_rule.legal_reviewed is False

    def test_rule_id_unique(self, vitamin_d_rule):
        with pytest.raises(IntegrityError):
            CrossDomainRule.objects.create(
                rule_id="vitamin_d_deficit_to_argan_massage",
                nutrition_trigger="low_iron",
                service_category_slug="other",
                insight_text_template="dup",
                rationale_text="dup",
                disclaimer_text="dup",
            )

    def test_default_cooldowns_match_po_decision(self, vitamin_d_rule):
        # PO-approved 2026-05-05: hybrid 30/14/7d cooldowns.
        assert vitamin_d_rule.cooldown_rule_days == 30
        assert vitamin_d_rule.cooldown_trigger_days == 14
        assert vitamin_d_rule.cooldown_category_days == 7
        assert vitamin_d_rule.skip_extend_days == 7
        assert vitamin_d_rule.double_skip_pause_days == 60

    def test_default_min_data_points(self, vitamin_d_rule):
        # Engine won't activate a rule unless the underlying pattern
        # has at least N data points — protects against single-day
        # flukes activating a 30-day cooldown.
        assert vitamin_d_rule.min_data_points == 3

    def test_default_excluded_health_flags_empty(self, vitamin_d_rule):
        # Each rule lists its own exclusion list. Vitamin D defaults
        # empty; iron-glow rule will list ["pregnant"] etc.
        assert vitamin_d_rule.excluded_health_flags == []

    def test_default_requires_premium_false(self, vitamin_d_rule):
        # Premium gating ships disabled (PO 2026-05-05). Toggle later
        # via Django admin without a migration.
        assert vitamin_d_rule.requires_premium is False


@pytest.mark.django_db
class TestCrossDomainShownRuleModel:
    def test_create_with_user_action_none(
        self, vitamin_d_rule, cross_domain_user,
    ):
        shown = CrossDomainShownRule.objects.create(
            user=cross_domain_user,
            rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc),
            surface="bot",
        )
        assert shown.user_action == "none"
        assert shown.seen_at is None
        assert shown.appointment is None

    def test_user_action_choices(self, vitamin_d_rule, cross_domain_user):
        # All five PO-approved actions must be valid.
        for action in (
            "none", "dismissed", "converted", "explained", "paused_7d",
        ):
            CrossDomainShownRule.objects.create(
                user=cross_domain_user,
                rule=vitamin_d_rule,
                nutrition_trigger="low_vitamin_d",
                service_category_slug="massage-argan-oil",
                shown_at=datetime.now(timezone.utc),
                surface="bot",
                user_action=action,
            )

    def test_surface_choices(self, vitamin_d_rule, cross_domain_user):
        # PO-approved 2026-05-05: bot now, mobile reserved for Phase 5.
        CrossDomainShownRule.objects.create(
            user=cross_domain_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        CrossDomainShownRule.objects.create(
            user=cross_domain_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="mobile",
        )

    def test_indexes_exist(self):
        # Cooldown queries scan by (user, -shown_at), (user, rule),
        # (user, nutrition_trigger). These need indexes for sub-ms reads.
        idx_fields = {
            tuple(idx.fields)
            for idx in CrossDomainShownRule._meta.indexes
        }
        assert ("user", "-shown_at") in idx_fields
        assert ("user", "rule") in idx_fields
        assert ("user", "nutrition_trigger") in idx_fields
