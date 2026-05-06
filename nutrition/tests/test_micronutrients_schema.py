"""DRF-260 — micronutrient schema extension.

Spec v2.0 (`POST /nutrition/scan` response) declares
``nutrition.vitamins: Record<string, number>`` and
``GET /nutrition/summary`` response declares
``vitamin_deficits: Record<string, number>`` but the underlying models
currently store macros only. Track E (cross-domain rule engine) needs
those vitamins/minerals snapshotted per FoodLog and per Beverage.

This test file fences the schema migration:

- 8 nullable micronutrient fields on ``FoodLog``, ``Beverage``,
  ``WaterEntry``: vitamin_d_iu, vitamin_b12_mcg, vitamin_c_mg, iron_mg,
  calcium_mg, magnesium_mg, omega3_g, fiber_g
- ``micronutrients_source`` choice on ``FoodLog`` describing which
  source filled the row (so Track E pattern engine can drop entries
  with too many AI-estimated values)
- ``DishMacros`` extension with optional micronutrient fields
- ``NutritionFacts`` extension exposing micronutrients to consumers
- Snapshot logic in ``FoodLogService`` that copies micronutrients from
  ``NutritionFacts`` into the ``FoodLog`` row at creation time

All micronutrient fields are ``null=True``: the migration is fully
backwards-compatible, and existing scans/logs/water entries remain
valid (with ``micronutrients_source="unknown"``).
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from nutrition.data.ru_dishes_seed import DISH_MACROS, DishMacros
from nutrition.models import Beverage, FoodLog, FoodScan, WaterEntry
from nutrition.services.food_log_service import (
    CreateFoodLogInput,
    FoodLogService,
)
from nutrition.services.nutrition_lookup import NutritionFacts


MICRONUTRIENT_FIELDS = (
    "vitamin_d_iu",
    "vitamin_b12_mcg",
    "vitamin_c_mg",
    "iron_mg",
    "calcium_mg",
    "magnesium_mg",
    "omega3_g",
    "fiber_g",
)


@pytest.fixture
def specialist_user(db, django_user_model):
    """Reusable user with a SpecialistProfile (signal-created) for FK targets."""
    return django_user_model.objects.create_user(
        username="micro_user", password="testpass123",
        role="client", phone="+79990300001",
    )


@pytest.mark.django_db
class TestFoodLogMicronutrientFields:
    """FoodLog now stores 8 micronutrient values + a provenance flag."""

    def _make_foodlog(self, user, **overrides) -> FoodLog:
        defaults = dict(
            user=user, dish_name="Test", portion_multiplier=1.0,
            calories=100, protein_g=5, fat_g=2, carbs_g=15,
            meal_type="snack", logged_at=timezone.now(),
        )
        defaults.update(overrides)
        return FoodLog.objects.create(**defaults)

    def test_eight_micronutrient_fields_default_null(self, specialist_user):
        log = self._make_foodlog(specialist_user)
        for field in MICRONUTRIENT_FIELDS:
            assert getattr(log, field) is None, (
                f"{field} should default to None (backwards-compat)"
            )

    def test_micronutrients_source_default_unknown(self, specialist_user):
        log = self._make_foodlog(specialist_user)
        assert log.micronutrients_source == "unknown"

    def test_micronutrients_source_choices(self, specialist_user):
        # The Choices class must include the five sources Track E uses.
        choices = {c[0] for c in FoodLog._meta.get_field(
            "micronutrients_source"
        ).choices}
        assert {
            "usda_full", "usda_partial", "rospotrebnadzor",
            "ai_estimate", "unknown",
        } <= choices

    def test_can_set_micronutrient_values(self, specialist_user):
        log = self._make_foodlog(
            specialist_user,
            vitamin_d_iu=120.0,
            iron_mg=14.0,
            omega3_g=0.8,
            micronutrients_source="rospotrebnadzor",
        )
        log.refresh_from_db()
        assert log.vitamin_d_iu == 120.0
        assert log.iron_mg == 14.0
        assert log.omega3_g == 0.8
        assert log.micronutrients_source == "rospotrebnadzor"


@pytest.mark.django_db
class TestBeverageMicronutrientFields:
    """Beverage stores per-100ml micronutrient values for juices, milk."""

    def test_eight_per_100ml_fields(self):
        bev = Beverage.objects.create(
            slug="orange-juice-test",
            name_ru="Апельсиновый сок (тест)",
            category="juice",
            water_coefficient=0.95,
            kcal_per_100ml=45,
            protein_g_per_100ml=0.7,
            fat_g_per_100ml=0.2,
            carbs_g_per_100ml=10.4,
        )
        for field in MICRONUTRIENT_FIELDS:
            field_per_100ml = f"{field}_per_100ml"
            assert hasattr(bev, field_per_100ml), (
                f"Beverage should have {field_per_100ml}"
            )
            assert getattr(bev, field_per_100ml) is None or \
                getattr(bev, field_per_100ml) == 0

    def test_orange_juice_vitamin_c_seedable(self):
        bev = Beverage.objects.create(
            slug="orange-juice-vitc",
            name_ru="Апельсиновый сок", category="juice",
            water_coefficient=0.95, kcal_per_100ml=45,
            vitamin_c_mg_per_100ml=40.0,
        )
        bev.refresh_from_db()
        assert bev.vitamin_c_mg_per_100ml == 40.0


@pytest.mark.django_db
class TestWaterEntryMicronutrientFields:
    """WaterEntry mirrors Beverage micronutrients at log time (snapshot)."""

    def test_eight_snapshot_fields_default_null(self, specialist_user):
        entry = WaterEntry.objects.create(
            user=specialist_user, ts=timezone.now(),
            ml=200, water_ml=200,
        )
        for field in MICRONUTRIENT_FIELDS:
            assert getattr(entry, field) is None


class TestNutritionFactsMicronutrients:
    """``NutritionFacts`` exposes optional micronutrients with default=None."""

    def test_facts_can_be_built_without_micronutrients(self):
        facts = NutritionFacts(
            matched_dish="борщ", source="seed_ru", portion_g=300,
            kcal_per_100g=49, protein_g_per_100g=1.6,
            fat_g_per_100g=2.2, carbs_g_per_100g=6.7,
            kcal=147, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
        )
        for field in MICRONUTRIENT_FIELDS:
            assert getattr(facts, field) is None
            assert getattr(facts, f"{field}_per_100g") is None

    def test_facts_can_carry_micronutrients(self):
        facts = NutritionFacts(
            matched_dish="борщ", source="seed_ru", portion_g=300,
            kcal_per_100g=49, protein_g_per_100g=1.6,
            fat_g_per_100g=2.2, carbs_g_per_100g=6.7,
            kcal=147, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
            iron_mg_per_100g=0.6, iron_mg=1.8,
            fiber_g_per_100g=1.7, fiber_g=5.1,
            micronutrients_source="rospotrebnadzor",
        )
        assert facts.iron_mg == 1.8
        assert facts.fiber_g == 5.1
        assert facts.micronutrients_source == "rospotrebnadzor"

    def test_to_dict_includes_micronutrients(self):
        facts = NutritionFacts(
            matched_dish="x", source="seed_ru", portion_g=100,
            kcal_per_100g=0, protein_g_per_100g=0,
            fat_g_per_100g=0, carbs_g_per_100g=0,
            kcal=0, protein_g=0, fat_g=0, carbs_g=0,
            vitamin_d_iu=10.0, vitamin_d_iu_per_100g=10.0,
        )
        d = facts.to_dict()
        assert d["vitamin_d_iu"] == 10.0
        assert d["vitamin_d_iu_per_100g"] == 10.0


class TestDishMacrosMicronutrients:
    """``DishMacros`` accepts optional micronutrient values."""

    def test_macros_default_micronutrients_to_none(self):
        m = DishMacros(
            kcal_per_100g=49, protein_g_per_100g=1.6,
            fat_g_per_100g=2.2, carbs_g_per_100g=6.7,
        )
        for field in MICRONUTRIENT_FIELDS:
            assert getattr(m, f"{field}_per_100g") is None

    def test_seed_borscht_iron_populated(self):
        # Borscht is in the curated top-N seed (Скурихин-Тутельян).
        # If it isn't, this test forces us to populate it.
        macros = DISH_MACROS.get("борщ")
        assert macros is not None
        assert macros.iron_mg_per_100g is not None, (
            "Track E requires top-20 RU dishes to carry at least iron, "
            "vitamin_d and fiber per Скурихин-Тутельян."
        )
        # Borscht has beets — iron > 0.
        assert macros.iron_mg_per_100g > 0


@pytest.mark.django_db
class TestFoodLogSnapshotsMicronutrients:
    """FoodLogService copies micronutrients into FoodLog at create time."""

    def _make_facts(self, **overrides):
        base = dict(
            matched_dish="борщ", source="seed_ru", portion_g=200,
            kcal_per_100g=49, protein_g_per_100g=1.6,
            fat_g_per_100g=2.2, carbs_g_per_100g=6.7,
            kcal=98, protein_g=3.2, fat_g=4.4, carbs_g=13.4,
            iron_mg_per_100g=0.6, iron_mg=1.2,
            fiber_g_per_100g=1.7, fiber_g=3.4,
            vitamin_d_iu_per_100g=0.0, vitamin_d_iu=0.0,
            micronutrients_source="rospotrebnadzor",
        )
        base.update(overrides)
        return NutritionFacts(**base)

    def test_manual_log_snapshots_micronutrients_from_lookup(
        self, specialist_user,
    ):
        # Use a real lookup against the seed (no mock):
        # a dish that the seed has micronutrients for.
        service = FoodLogService()
        log = service.create(CreateFoodLogInput(
            user_id=specialist_user.id,
            portion_multiplier=1.0,
            meal_type="lunch",
            dish_name="борщ",
        ))
        # Borscht seed has iron — log should carry it.
        assert log.iron_mg is not None
        assert log.iron_mg > 0
        assert log.micronutrients_source == "rospotrebnadzor"

    def test_scan_path_snapshots_micronutrients_from_scan_nutrition(
        self, specialist_user,
    ):
        # Build a scan whose nutrition JSON carries micronutrients.
        scan = FoodScan.objects.create(
            user=specialist_user,
            dish_name="борщ", confidence=0.9, portion_g=300,
            nutrition={
                "kcal": 147, "protein_g": 4.8, "fat_g": 6.6, "carbs_g": 20.1,
                "iron_mg": 1.8, "fiber_g": 5.1,
                "micronutrients_source": "rospotrebnadzor",
            },
        )
        service = FoodLogService()
        log = service.create(CreateFoodLogInput(
            user_id=specialist_user.id,
            scan_id=scan.id,
            portion_multiplier=1.0,
            meal_type="lunch",
        ))
        assert log.iron_mg == 1.8
        assert log.fiber_g == 5.1
        assert log.micronutrients_source == "rospotrebnadzor"

    def test_scan_path_micronutrients_scaled_by_portion_multiplier(
        self, specialist_user,
    ):
        scan = FoodScan.objects.create(
            user=specialist_user,
            dish_name="борщ", confidence=0.9, portion_g=300,
            nutrition={
                "kcal": 147, "protein_g": 4.8, "fat_g": 6.6, "carbs_g": 20.1,
                "iron_mg": 1.8, "fiber_g": 5.1,
                "micronutrients_source": "rospotrebnadzor",
            },
        )
        service = FoodLogService()
        log = service.create(CreateFoodLogInput(
            user_id=specialist_user.id,
            scan_id=scan.id,
            portion_multiplier=0.5,
            meal_type="lunch",
        ))
        # _scale_micro rounds to 2dp (micros are often <1 — rounding to 1dp
        # would drop precision for vit B12 µg / iron mg).
        assert log.iron_mg == round(1.8 * 0.5, 2)
        assert log.fiber_g == round(5.1 * 0.5, 2)

    def test_log_without_micronutrients_data_marks_unknown(
        self, specialist_user,
    ):
        # A scan with no micronutrient keys shouldn't crash, just leave
        # micronutrients=None and source=unknown.
        scan = FoodScan.objects.create(
            user=specialist_user,
            dish_name="борщ", confidence=0.9, portion_g=300,
            nutrition={
                "kcal": 147, "protein_g": 4.8, "fat_g": 6.6, "carbs_g": 20.1,
            },
        )
        service = FoodLogService()
        log = service.create(CreateFoodLogInput(
            user_id=specialist_user.id,
            scan_id=scan.id,
            portion_multiplier=1.0,
            meal_type="lunch",
        ))
        assert log.iron_mg is None
        assert log.fiber_g is None
        assert log.micronutrients_source == "unknown"
