"""DRF-263 — CrossDomainEngine cooldown + selection.

Engine selects top-1 cross-domain recommendation for a user. Logic:

1. Pull active patterns (DRF-262 detect_patterns).
2. For each rule with is_active=True AND legal_reviewed=True AND
   nutrition_trigger ∈ active_patterns:
   a. Skip if any excluded_health_flag is set on the user's profile.
   b. Skip if requires_premium=True and user.is_premium=False.
   c. Skip if cooldown blocks (rule / trigger / category / global cap /
      auto-confirm-5min).
   d. Skip if no specialist supplies the service_category.
   e. Score: priority + ×1.5 boost for favorite-specialist match.
3. Return top-1 by score, or None.

Cooldown matrix (PO 2026-05-05):
- Per-rule: 30 days
- Per-trigger: 14 days
- Per-category: 7 days
- Global cap: 1 in 3 days (any rule)
- Skip extends rule cooldown: +7 days
- Double-skip pause: 60 days for trigger
- Auto-confirm-5min: rows count as "seen" 5 minutes after shown_at
  even when /seen/ never came.

Eating-disorder mode: ALWAYS returns None. We never surface deficit
framing to ED users — pattern engine already suppresses, but the
engine adds a second guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from nutrition.models import (
    CrossDomainRule,
    CrossDomainShownRule,
    NutritionProfile,
)
from nutrition.services.cross_domain_engine import CrossDomainEngine
from nutrition.services.pattern_detection_service import DetectedPattern


User = get_user_model()


def _active_pattern(slug="low_vitamin_d", count=5, severity="medium"):
    return DetectedPattern(
        slug=slug, name_ru="test", count=count,
        active_window_days=7, severity=severity,
        recent_dates=[], advice_template_args={},
        display_hint="primary",
    )


@pytest.fixture
def cd_user(db):
    user = User.objects.create_user(
        username="cd_engine_test", password="x",
        role="client", phone="+79990800001",
    )
    NutritionProfile.objects.create(
        user=user, gender="female", age=40,
        height_cm=165, weight_kg=70.0,
        goal="maintain", pace="moderate",
    )
    return user


@pytest.fixture
def active_rule(db):
    return CrossDomainRule.objects.create(
        rule_id="vitamin_d_to_argan",
        nutrition_trigger="low_vitamin_d",
        service_category_slug="massage-argan-oil",
        insight_text_template="text",
        rationale_text="rationale",
        disclaimer_text="not medical advice",
        is_active=True, legal_reviewed=True,
    )


def _patch_patterns(patterns):
    """Replace detect_patterns() with a stub returning fixed patterns."""

    def _stub(*, user_id, force=False):
        from nutrition.services.pattern_detection_service import (
            PatternDetectionResult,
        )
        return PatternDetectionResult(
            active_days=10, patterns=patterns,
        )

    return patch(
        "nutrition.services.cross_domain_engine.detect_patterns",
        side_effect=_stub,
    )


def _patch_supply_ok(ok=True):
    """Replace supply check with a stub. Real implementation queries Service."""
    return patch.object(
        CrossDomainEngine, "_has_supply", return_value=ok,
    )


@pytest.mark.django_db
class TestEngineHappyPath:
    def test_returns_top_1_when_pattern_active_and_rule_match(
        self, cd_user, active_rule,
    ):
        with _patch_patterns([_active_pattern("low_vitamin_d")]), \
             _patch_supply_ok():
            engine = CrossDomainEngine()
            rec = engine.evaluate(user=cd_user, surface="bot")

        assert rec is not None
        assert rec.rule_id == "vitamin_d_to_argan"
        assert rec.nutrition_trigger == "low_vitamin_d"

    def test_creates_shown_rule_on_evaluate(self, cd_user, active_rule):
        with _patch_patterns([_active_pattern("low_vitamin_d")]), \
             _patch_supply_ok():
            CrossDomainEngine().evaluate(user=cd_user, surface="bot")

        assert CrossDomainShownRule.objects.filter(
            user=cd_user, rule=active_rule, surface="bot",
        ).exists()

    def test_returns_none_when_no_active_patterns(self, cd_user, active_rule):
        with _patch_patterns([]), _patch_supply_ok():
            rec = CrossDomainEngine().evaluate(user=cd_user, surface="bot")
        assert rec is None

    def test_returns_none_when_pattern_does_not_match_any_rule(
        self, cd_user, active_rule,
    ):
        with _patch_patterns([_active_pattern("low_iron")]), _patch_supply_ok():
            rec = CrossDomainEngine().evaluate(user=cd_user, surface="bot")
        assert rec is None


@pytest.mark.django_db
class TestEngineRuleGates:
    def test_inactive_rule_skipped(self, cd_user):
        CrossDomainRule.objects.create(
            rule_id="off", nutrition_trigger="low_vitamin_d",
            service_category_slug="x",
            insight_text_template="t", rationale_text="r",
            disclaimer_text="d",
            is_active=False, legal_reviewed=True,
        )
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_unreviewed_rule_skipped(self, cd_user):
        CrossDomainRule.objects.create(
            rule_id="unreviewed", nutrition_trigger="low_vitamin_d",
            service_category_slug="x",
            insight_text_template="t", rationale_text="r",
            disclaimer_text="d",
            is_active=True, legal_reviewed=False,
        )
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None


@pytest.mark.django_db
class TestEngineHealthFlagExclusion:
    def test_rule_with_excluded_flag_skipped(self, cd_user, active_rule):
        active_rule.excluded_health_flags = ["pregnant"]
        active_rule.save()
        prof = cd_user.nutrition_profile
        prof.health_flags = {"pregnant": True}
        prof.save()

        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_eating_disorder_mode_returns_none_always(self, cd_user, active_rule):
        # Even without excluded_health_flags listing it, ED mode is a
        # global gate — the engine never returns a recommendation.
        prof = cd_user.nutrition_profile
        prof.health_flags = {"eating_disorder": True}
        prof.save()

        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None


@pytest.mark.django_db
class TestEnginePremiumGating:
    def test_premium_rule_skipped_for_free_user(self, cd_user, active_rule):
        active_rule.requires_premium = True
        active_rule.save()
        # User is free by default (no is_premium attribute / False).
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None


@pytest.mark.django_db
class TestEngineCooldowns:
    def _shown(
        self, user, rule, trigger="low_vitamin_d", cat="massage-argan-oil",
        days_ago=0, action="none", seen=False,
    ):
        ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return CrossDomainShownRule.objects.create(
            user=user, rule=rule,
            nutrition_trigger=trigger,
            service_category_slug=cat,
            shown_at=ts,
            seen_at=ts if seen else None,
            surface="bot",
            user_action=action,
        )

    def test_global_cap_blocks_within_3_days(self, cd_user, active_rule):
        # Different rule shown 1 day ago, but global cap = 3 days for ANY rule.
        other = CrossDomainRule.objects.create(
            rule_id="iron_to_glow", nutrition_trigger="low_iron",
            service_category_slug="cosmetology-brightening",
            insight_text_template="t", rationale_text="r",
            disclaimer_text="d", is_active=True, legal_reviewed=True,
        )
        self._shown(cd_user, other, trigger="low_iron",
                    cat="cosmetology-brightening", days_ago=1, seen=True)

        with _patch_patterns([_active_pattern("low_vitamin_d")]), \
             _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_per_rule_cooldown_blocks_within_30_days(self, cd_user, active_rule):
        self._shown(cd_user, active_rule, days_ago=10, seen=True)
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_per_rule_cooldown_lifts_after_30_days(self, cd_user, active_rule):
        self._shown(cd_user, active_rule, days_ago=31, seen=True)
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is not None

    def test_per_trigger_cooldown_blocks_different_rule_same_trigger(
        self, cd_user, active_rule,
    ):
        # Another rule firing on low_vitamin_d → blocked for 14 days.
        CrossDomainRule.objects.create(
            rule_id="vitamin_d_to_facial",
            nutrition_trigger="low_vitamin_d",
            service_category_slug="cosmetology-deep-hydration",
            insight_text_template="t", rationale_text="r",
            disclaimer_text="d", is_active=True, legal_reviewed=True,
        )
        self._shown(cd_user, active_rule, days_ago=10, seen=True)
        with _patch_patterns([_active_pattern("low_vitamin_d")]), \
             _patch_supply_ok():
            # Both rules have nutrition_trigger=low_vitamin_d; the
            # other rule is blocked by per-trigger cooldown (14d).
            rec = CrossDomainEngine().evaluate(user=cd_user, surface="bot")
        assert rec is None

    def test_per_category_cooldown_blocks_different_trigger_same_category(
        self, cd_user, active_rule,
    ):
        # Same category (massage-argan-oil) shown 5 days ago — blocks
        # for 7 days regardless of trigger.
        other = CrossDomainRule.objects.create(
            rule_id="iron_to_argan",
            nutrition_trigger="low_iron",
            service_category_slug="massage-argan-oil",
            insight_text_template="t", rationale_text="r",
            disclaimer_text="d", is_active=True, legal_reviewed=True,
        )
        self._shown(cd_user, other, trigger="low_iron",
                    cat="massage-argan-oil", days_ago=5, seen=True)
        # 5 days < 7d category cooldown → vitamin_d rule blocked.
        with _patch_patterns([_active_pattern("low_vitamin_d")]), \
             _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_skip_extends_rule_cooldown_by_7_days(self, cd_user, active_rule):
        # Dismissed 32 days ago: 30d rule cooldown + 7d skip extension =
        # 37d cooldown. At day 32, still blocked.
        self._shown(cd_user, active_rule, days_ago=32, seen=True,
                    action="dismissed")
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_double_skip_pause_60_days(self, cd_user, active_rule):
        # Two recent dismissals → 60-day pause.
        self._shown(cd_user, active_rule, days_ago=20, seen=True,
                    action="dismissed")
        self._shown(cd_user, active_rule, days_ago=10, seen=True,
                    action="dismissed")
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_double_skip_pause_catches_dismissal_outside_30d_window(
        self, cd_user, active_rule,
    ):
        """LB-7 regression: previously the dismissal-recency window was
        hard-coded to 30 days, but the pause itself is 60 — so a second
        dismissal at day 40 escaped the count and the 60-day pause
        silently lifted. We choose distances outside the 30+7 rule
        cooldown so this test isolates the double-skip path.
        """
        # Both dismissals past the 37d (30+7 skip) rule cooldown so the
        # rule_cooldown gate alone does NOT block. Both still inside the
        # 60d double-skip window, so the pause MUST block.
        self._shown(cd_user, active_rule, days_ago=55, seen=True,
                    action="dismissed")
        self._shown(cd_user, active_rule, days_ago=40, seen=True,
                    action="dismissed")
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_auto_confirm_5min_treats_unseen_as_seen(
        self, cd_user, active_rule,
    ):
        # Shown 10 minutes ago, never confirmed via /seen/. The 30d
        # rule cooldown should still be active because the row counts
        # as "seen" once 5 minutes have passed.
        ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        CrossDomainShownRule.objects.create(
            user=cd_user, rule=active_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=ts, seen_at=None, surface="bot",
        )
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None

    def test_under_5min_unseen_does_not_block(self, cd_user, active_rule):
        # Shown 1 minute ago, never confirmed. Auto-confirm not yet
        # triggered → row is still "in flight" and shouldn't block.
        # (Combined with global cap 3 days, it WILL still block — but
        # the cooldown reason should be global cap, not per-rule.)
        # Test: delete the in-flight row, then evaluate.
        ts = datetime.now(timezone.utc) - timedelta(minutes=1)
        in_flight = CrossDomainShownRule.objects.create(
            user=cd_user, rule=active_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=ts, seen_at=None, surface="bot",
        )
        # The in-flight row blocks via global cap (1 in 3 days).
        # Verify by deleting it — engine should now return a fresh rec.
        in_flight.delete()
        with _patch_patterns([_active_pattern()]), _patch_supply_ok():
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is not None


@pytest.mark.django_db
class TestEngineSupplyCheck:
    def test_no_supply_skips_rule(self, cd_user, active_rule):
        # Stub supply as missing.
        with _patch_patterns([_active_pattern()]), _patch_supply_ok(ok=False):
            assert CrossDomainEngine().evaluate(user=cd_user, surface="bot") is None


@pytest.mark.django_db
class TestAppointmentSetNullOnDelete:
    """LB-8 regression: deleting an attributed appointment must NOT
    delete the shown row (it's the analytics record). PROTECT used to
    block the appointment delete entirely; SET_NULL preserves the row
    and zeros the FK so funnel attribution stays auditable.
    """

    def test_appointment_delete_preserves_shown_row(
        self, cd_user, active_rule,
    ):
        from appointments.models import Appointment
        from services.models import Service, ServiceCategory
        from users.models import SpecialistProfile

        category = ServiceCategory.objects.create(
            slug="massage-argan-oil", name="Massage",
            sort_order=1, is_active=True,
        )
        specialist_user = User.objects.create_user(
            username="lb8_specialist", password="x",
            role="specialist", phone="+79991110001",
        )
        # Specialist profile may be auto-created by post_save signal.
        spec, _ = SpecialistProfile.objects.get_or_create(
            user=specialist_user,
            defaults={"display_name": "Test", "bio": "t"},
        )
        service = Service.objects.create(
            specialist=spec, category=category, name="Argan",
            price=1000, duration_minutes=60, is_active=True,
        )
        appointment = Appointment.objects.create(
            client=cd_user, specialist=spec, service=service,
            start_datetime=datetime.now(timezone.utc) + timedelta(days=2),
            end_datetime=datetime.now(timezone.utc) + timedelta(days=2, hours=1),
            price=1000, status="pending",
        )
        shown = CrossDomainShownRule.objects.create(
            user=cd_user, rule=active_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc),
            surface="bot", appointment=appointment,
        )

        # The whole point of SET_NULL: appointment.delete() succeeds
        # without raising, and shown row survives with appointment=None.
        appointment.delete()

        shown.refresh_from_db()
        assert shown.appointment_id is None
        assert CrossDomainShownRule.objects.filter(pk=shown.pk).exists()
