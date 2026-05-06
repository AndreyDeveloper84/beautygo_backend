"""DRF-264 — Spec v2.0 vitamins + beauty_insights wire shape.

Notion API Specification v2.0 §FOOD SCANNER declares:
- ``FoodScanResponse.nutrition.vitamins: Record<string, number>`` (mg)
- ``FoodScanResponse.beauty_insights: { vitamin_deficits, beauty_impact,
  recommendation } | null``

Notion §FOOD SCANNER+NUTRITION declares:
- ``NutritionSummaryResponse.vitamin_deficits: Record<string, number>``
  (% of RDA)

DRF-260 added micronutrient fields to FoodScan.nutrition JSON. DRF-263
added the cross-domain rule engine. This ticket wires both into the
public response shape.

Inline cooldown bookkeeping (PO-approved 2026-05-05): when /scan/
yields ``beauty_insights != null``, the engine creates a
CrossDomainShownRule row immediately. Auto-confirm-5min handles the
case where mobile/bot never POSTs /seen/.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from nutrition.models import (
    CrossDomainRule,
    CrossDomainShownRule,
    FoodScan,
    NutritionProfile,
)
from nutrition.serializers import (
    FoodScanResponseSerializer,
    NutritionSummaryResponseSerializer,
)
from nutrition.services.nutrition_summary_service import (
    NutritionSummary,
    SummaryTotals,
)
from nutrition.services.pattern_detection_service import DetectedPattern


User = get_user_model()


@pytest.fixture
def scan_user(db):
    user = User.objects.create_user(
        username="spec_v2_user", password="x",
        role="client", phone="+79991100001",
    )
    NutritionProfile.objects.create(
        user=user, gender="female", age=40,
        height_cm=165, weight_kg=70.0,
        goal="maintain", pace="moderate",
    )
    return user


@pytest.fixture
def vitamin_d_rule(db):
    return CrossDomainRule.objects.create(
        rule_id="vitamin_d_to_argan_for_test",
        nutrition_trigger="low_vitamin_d",
        service_category_slug="massage-argan-oil",
        insight_text_template=(
            "За последние дни витамин D ниже нормы — это часто "
            "отражается на коже"
        ),
        rationale_text=(
            "Массаж с аргановым маслом помогает коже"
        ),
        disclaimer_text="Не медицинская рекомендация",
        is_active=True, legal_reviewed=True,
    )


@pytest.mark.django_db
class TestFoodScanVitaminsShape:
    """Spec v2.0 §FoodScanResponse: ``nutrition.vitamins`` Record<str, num>."""

    def test_vitamins_populated_from_scan_nutrition_json(self, scan_user):
        scan = FoodScan.objects.create(
            user=scan_user,
            dish_name="Apple",
            confidence=0.9,
            portion_g=100,
            nutrition={
                "kcal": 52, "protein_g": 0.3, "fat_g": 0.2, "carbs_g": 14.0,
                "vitamin_c_mg": 4.6,
                "iron_mg": 0.12,
                "vitamin_d_iu": 0.0,
                "calcium_mg": 6.0,
            },
        )
        data = FoodScanResponseSerializer(scan).data
        nutrition = data["nutrition"]
        assert nutrition is not None
        # Spec v2.0: vitamins is a Record<string, number> map
        assert "vitamins" in nutrition
        vitamins = nutrition["vitamins"]
        # Track E micronutrients exposed when present
        assert vitamins.get("vitamin_c_mg") == 4.6
        assert vitamins.get("iron_mg") == 0.12
        assert vitamins.get("calcium_mg") == 6.0

    def test_vitamins_empty_when_scan_only_has_macros(self, scan_user):
        # Backwards compat: legacy scans without micronutrient keys
        # still serialize cleanly with an empty vitamins map.
        scan = FoodScan.objects.create(
            user=scan_user, dish_name="Old", confidence=0.9, portion_g=100,
            nutrition={
                "kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 15,
            },
        )
        data = FoodScanResponseSerializer(scan).data
        assert data["nutrition"]["vitamins"] == {}

    def test_vitamins_omits_null_values(self, scan_user):
        # Partial USDA result — some keys present, others null. Null
        # values must NOT pollute the response (mobile would render
        # "0 mg" or similar). Keep the map sparse.
        scan = FoodScan.objects.create(
            user=scan_user, dish_name="Partial", confidence=0.9,
            portion_g=100,
            nutrition={
                "kcal": 200, "protein_g": 10, "fat_g": 5, "carbs_g": 25,
                "iron_mg": 1.5,
                "vitamin_d_iu": None,   # provider didn't supply
                "omega3_g": None,
            },
        )
        data = FoodScanResponseSerializer(scan).data
        vitamins = data["nutrition"]["vitamins"]
        assert vitamins.get("iron_mg") == 1.5
        assert "vitamin_d_iu" not in vitamins
        assert "omega3_g" not in vitamins


@pytest.mark.django_db
class TestFoodScanBeautyInsights:
    """Spec v2.0: beauty_insights either populated dict or null."""

    def _stub_engine(self, recommendation):
        from nutrition.services.cross_domain_engine import (
            CrossDomainRecommendation,
        )
        if recommendation is None:
            return patch(
                "nutrition.serializers.evaluate_for_scan",
                return_value=None,
            )
        return patch(
            "nutrition.serializers.evaluate_for_scan",
            return_value=CrossDomainRecommendation(**recommendation),
        )

    def test_beauty_insights_null_when_engine_returns_none(self, scan_user):
        scan = FoodScan.objects.create(
            user=scan_user, dish_name="x", confidence=0.9,
            portion_g=100,
            nutrition={"kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 15},
        )
        with self._stub_engine(None):
            data = FoodScanResponseSerializer(
                scan, context={"request": None},
            ).data
        assert data["beauty_insights"] is None

    def test_beauty_insights_populated_with_spec_v2_keys(self, scan_user):
        scan = FoodScan.objects.create(
            user=scan_user, dish_name="x", confidence=0.9,
            portion_g=100,
            nutrition={"kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 15},
        )
        with self._stub_engine({
            "shown_id": "00000000-0000-0000-0000-000000000001",
            "rule_id": "vitamin_d_to_argan_for_test",
            "nutrition_trigger": "low_vitamin_d",
            "service_category_slug": "massage-argan-oil",
            "insight_text": "За последние 5 дней витамин D ниже нормы",
            "rationale_text": "Массаж с аргановым маслом помогает коже",
            "disclaimer_text": "Не медицинская рекомендация",
            "data_points": 5,
            "severity": "medium",
        }):
            data = FoodScanResponseSerializer(
                scan, context={"request": None},
            ).data

        bi = data["beauty_insights"]
        assert bi is not None
        # Spec v2.0 keys: vitamin_deficits, beauty_impact, recommendation
        assert "vitamin_deficits" in bi
        assert "beauty_impact" in bi
        assert "recommendation" in bi
        # vitamin_deficits is a string array per spec v2.0.
        # LB-9 regression: list must contain the nutrient name
        # ("vitamin_d"), NOT the internal pattern slug ("low_vitamin_d").
        # Mobile/bot consume this verbatim — leaking ``low_`` ruins the UI.
        assert isinstance(bi["vitamin_deficits"], list)
        assert bi["vitamin_deficits"] == ["vitamin_d"]
        assert "low_" not in bi["vitamin_deficits"][0]


@pytest.mark.django_db
class TestNutritionSummaryVitaminDeficits:
    """Spec v2.0 §GET /nutrition/summary: vitamin_deficits is Record<str, num> (% RDA)."""

    def _make_summary(self, vitamin_deficits=None):
        return NutritionSummary(
            date=datetime.now(timezone.utc).date(),
            totals=SummaryTotals(
                calories=1500.0, protein_g=80.0, fat_g=50.0, carbs_g=180.0,
            ),
            calories_goal=2000,
            water_ml=1500, water_goal_ml=2000,
            entries=[],
            vitamin_deficits=vitamin_deficits or {},
        )

    def test_empty_vitamin_deficits_serialize_as_empty_dict(self):
        summary = self._make_summary({})
        data = NutritionSummaryResponseSerializer(summary).data
        assert data["vitamin_deficits"] == {}

    def test_populated_vitamin_deficits(self):
        # Per spec v2.0: % of RDA per micronutrient. Mobile renders
        # the bars from this dict.
        summary = self._make_summary({
            "vitamin_d": 22.0,   # 22% of RDA
            "iron": 78.0,
            "omega3": 31.0,
            "calcium": 80.0,
        })
        data = NutritionSummaryResponseSerializer(summary).data
        deficits = data["vitamin_deficits"]
        assert deficits["vitamin_d"] == 22.0
        assert deficits["omega3"] == 31.0


@pytest.mark.django_db
class TestInlineCooldownBookkeeping:
    """When /scan/ returns beauty_insights, engine writes ShownRule row."""

    def test_scan_with_active_pattern_creates_shown_rule(
        self, scan_user, vitamin_d_rule,
    ):
        scan = FoodScan.objects.create(
            user=scan_user, dish_name="x", confidence=0.9,
            portion_g=100,
            nutrition={
                "kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 15,
            },
        )

        # Stub detect_patterns to return an active low_vitamin_d.
        active = DetectedPattern(
            slug="low_vitamin_d", name_ru="Дефицит D", count=5,
            active_window_days=7, severity="medium",
            recent_dates=[], advice_template_args={}, display_hint="primary",
        )
        with patch(
            "nutrition.services.cross_domain_engine.detect_patterns",
        ) as mock_dp, patch(
            "nutrition.services.cross_domain_engine.CrossDomainEngine."
            "_has_supply",
            return_value=True,
        ):
            from nutrition.services.pattern_detection_service import (
                PatternDetectionResult,
            )
            mock_dp.return_value = PatternDetectionResult(
                active_days=10, patterns=[active],
            )
            data = FoodScanResponseSerializer(
                scan, context={"request": None},
            ).data

        # beauty_insights populated → ShownRule row exists.
        assert data["beauty_insights"] is not None
        assert CrossDomainShownRule.objects.filter(
            user=scan_user, rule=vitamin_d_rule,
        ).exists()
