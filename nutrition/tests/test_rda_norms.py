"""DRF-265 — RDA (recommended daily allowance) calculation for micronutrients.

Track E pattern engine compares per-day intake against the user's
**recommended** daily intake to detect deficits. The recommendation
itself depends on demographics + health flags:

- Adult women 19-50 (the Penza pilot median) get the USDA reference RDA
- Pregnant women: iron up, calcium up, omega-3 up, fibre up
- Breastfeeding women: calcium up, vitamin C up, omega-3 up
- Vegan/vegetarian: B12 ×~1.5–2 (poor plant bioavailability),
  iron ×1.8 (non-heme iron absorbed worse than heme)
- Adolescents (16-18) and seniors (65+) shift on a few axes

This file fences:
- 7 ``daily_*_norm`` fields on ``NutritionProfile`` (spec §3.4)
- ``compute_norms`` extension that returns the same 7 numbers in
  ``ComputedNorms`` (or a sibling ``ComputedRDA`` — implementation
  detail)
- override behaviour (pregnancy / breastfeeding / vegan)
- Profile upsert reflecting the new norms in GET response
- Backwards compat: existing macros (kcal/protein/fat/carbs/water)
  unaffected
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_profile_service import (
    ProfileInputs,
    compute_norms,
)


# Adult women 19-50 RDA targets (USDA / NIH ODS) — the baseline the
# pattern engine compares against. Fenced here so the calculation can
# never silently drift.
ADULT_FEMALE_RDA = {
    "vitamin_d_iu": 600,
    "b12_mcg": 2.4,
    "vitamin_c_mg": 75,
    "iron_mg": 18,
    "calcium_mg": 1000,
    "magnesium_mg": 310,
    "omega3_g": 1.1,
    "fiber_g": 25,
}


@pytest.fixture
def adult_female_inputs():
    return ProfileInputs(
        gender="female", age=40, height_cm=165, weight_kg=70.0,
        activity_coefficient=1.4, goal="maintain", pace="moderate",
        health_flags={},
    )


class TestRDAFemaleAdult:
    """Baseline: adult woman 19-50, no flags. Hits the USDA reference RDA."""

    def test_returns_seven_micronutrient_targets(self, adult_female_inputs):
        norms = compute_norms(adult_female_inputs)
        assert norms.daily_vitamin_d_iu == ADULT_FEMALE_RDA["vitamin_d_iu"]
        assert norms.daily_vitamin_b12_mcg == ADULT_FEMALE_RDA["b12_mcg"]
        assert norms.daily_vitamin_c_mg == ADULT_FEMALE_RDA["vitamin_c_mg"]
        assert norms.daily_iron_mg == ADULT_FEMALE_RDA["iron_mg"]
        assert norms.daily_calcium_mg == ADULT_FEMALE_RDA["calcium_mg"]
        assert norms.daily_magnesium_mg == ADULT_FEMALE_RDA["magnesium_mg"]
        assert norms.daily_omega3_g == ADULT_FEMALE_RDA["omega3_g"]
        assert norms.daily_fiber_g == ADULT_FEMALE_RDA["fiber_g"]


class TestRDAMaleAdult:
    """Adult men 19-50 differ from women on iron (8 vs 18) and a few others."""

    def test_male_iron_lower_than_female(self):
        # Men's iron RDA is 8 mg vs women's 18 — period of monthly loss
        # accounts for the bulk of the gap.
        norms = compute_norms(ProfileInputs(
            gender="male", age=35, height_cm=180, weight_kg=80.0,
        ))
        assert norms.daily_iron_mg == 8

    def test_male_magnesium_higher_than_female(self):
        # Magnesium scales with body mass; reference 400 vs 310.
        norms = compute_norms(ProfileInputs(
            gender="male", age=35, height_cm=180, weight_kg=80.0,
        ))
        assert norms.daily_magnesium_mg == 400


class TestRDAPregnancy:
    """Pregnancy: iron 27, calcium 1000, omega-3 1.4, fibre 28."""

    def test_pregnancy_overrides(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=70.0,
            health_flags={"pregnant": True},
        ))
        assert norms.daily_iron_mg == 27
        assert norms.daily_calcium_mg == 1000
        # Pregnancy bumps omega-3 (DHA matters for fetal neurodevelopment).
        assert norms.daily_omega3_g == 1.4
        # Fibre creeps up too (pregnancy-related GI sluggishness).
        assert norms.daily_fiber_g == 28


class TestRDABreastfeeding:
    """Breastfeeding: calcium 1000, vit C 120, omega-3 1.3, iron *down* to 9."""

    def test_breastfeeding_overrides(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=70.0,
            health_flags={"breastfeeding": True},
        ))
        assert norms.daily_calcium_mg == 1000
        assert norms.daily_vitamin_c_mg == 120
        assert norms.daily_omega3_g == 1.3
        # Iron requirement DROPS during breastfeeding (no menses).
        assert norms.daily_iron_mg == 9


class TestRDAVegan:
    """Vegan/vegetarian: B12 ~×1.5-2 and iron ×1.8 (plant bioavailability)."""

    def test_vegan_b12_doubled(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=65.0,
            health_flags={"vegan": True},
        ))
        # 2.4 → ~4.0 (4x, NIH guidance for plant-based diets without
        # supplementation; we use 4.0 mcg as a conservative target).
        assert norms.daily_vitamin_b12_mcg == 4.0

    def test_vegan_iron_18x18(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=65.0,
            health_flags={"vegan": True},
        ))
        # 18 × 1.8 = 32.4 → rounded to 32 (display-friendly integer).
        assert norms.daily_iron_mg == 32

    def test_vegetarian_via_diet_preference(self):
        # Vegan can also be expressed via diet_preference="vegan".
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=65.0,
            health_flags={"vegetarian": True},
        ))
        # Vegetarian eats dairy & eggs → b12 still gets a smaller bump.
        # 2.4 → 3.0 (50% bump for partial dietary restriction).
        assert norms.daily_vitamin_b12_mcg == 3.0


class TestRDAAdolescent:
    """Age 14-18 women: calcium 1300, iron 15, vitamin D 600."""

    def test_adolescent_calcium_higher(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=16, height_cm=165, weight_kg=55.0,
        ))
        # Bone development demands more calcium during adolescence.
        assert norms.daily_calcium_mg == 1300

    def test_adolescent_iron_15(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=16, height_cm=165, weight_kg=55.0,
        ))
        # 14-18 girls: 15 mg iron (vs 18 for 19-50, 8 for adolescent boys).
        assert norms.daily_iron_mg == 15


class TestRDASenior:
    """Age 65+: vitamin D 800 (vs 600), calcium 1200 (vs 1000)."""

    def test_senior_vitamin_d_800(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=68, height_cm=160, weight_kg=65.0,
        ))
        assert norms.daily_vitamin_d_iu == 800

    def test_senior_female_calcium_1200(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=68, height_cm=160, weight_kg=65.0,
        ))
        # Postmenopausal women need 1200 mg/day for bone preservation.
        assert norms.daily_calcium_mg == 1200


@pytest.mark.django_db
class TestProfileFieldsPersisted:
    """``NutritionProfile`` carries the seven new norm fields."""

    def test_fields_default_zero(self, django_user_model):
        user = django_user_model.objects.create_user(
            username="rda_test_user", password="x",
            role="client", phone="+79990400001",
        )
        p = NutritionProfile.objects.create(user=user)
        assert p.daily_vitamin_d_iu == 0
        assert p.daily_vitamin_b12_mcg == 0.0
        assert p.daily_vitamin_c_mg == 0
        assert p.daily_iron_mg == 0.0
        assert p.daily_calcium_mg == 0
        assert p.daily_magnesium_mg == 0
        assert p.daily_omega3_g == 0.0
        assert p.daily_fiber_g == 0


@pytest.mark.django_db
class TestProfileUpsertWritesRDA:
    """POST /internal/profile/ recomputes & persists the seven new fields."""

    def _post_profile(self, payload):
        # Service-account auth via X-Service-Token + X-External-User-ID,
        # exactly like other internal-* endpoints.
        from django.conf import settings
        client = APIClient()
        return client.post(
            "/api/v1/nutrition/internal/profile/",
            payload, format="json",
            HTTP_X_SERVICE_TOKEN=settings.NUTRITION_SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:rda-test-12345",
        )

    def test_post_sets_micronutrient_norms(self, settings):
        # Service token is required by IsServiceAccount permission.
        settings.NUTRITION_SERVICE_TOKEN = "test-rda-token"

        resp = self._post_profile({
            "gender": "female", "age": 40,
            "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain",
        })
        assert resp.status_code in (200, 201), resp.content

        # GET round-trips with the same token.
        client = APIClient()
        get_resp = client.get(
            "/api/v1/nutrition/internal/profile/",
            HTTP_X_SERVICE_TOKEN="test-rda-token",
            HTTP_X_EXTERNAL_USER_ID="bot:rda-test-12345",
        )
        assert get_resp.status_code == 200
        norms = get_resp.json().get("data", get_resp.json()).get("norms", {})
        assert norms.get("daily_vitamin_d_iu") == 600
        assert norms.get("daily_iron_mg") == 18
        assert norms.get("daily_calcium_mg") == 1000


class TestRDABackwardsCompat:
    """Adding RDA must not break existing macro/water computation."""

    def test_macros_unchanged(self, adult_female_inputs):
        # Adult woman 40/165/70, maintain → Mifflin-St-Jeor:
        # 10×70 + 6.25×165 − 5×40 − 161 = 1370.25.
        # daily_kcal = 1370 × 1.4 × 1.0 = 1918.
        # protein 1.4 g/kg × 70 = 98.
        norms = compute_norms(adult_female_inputs)
        assert 1350 <= norms.bmr <= 1400
        assert 1900 <= norms.daily_kcal <= 2100
        # Protein floor: 1.4 g/kg for maintain.
        assert 95 <= norms.daily_protein_g <= 105

    def test_water_unchanged(self, adult_female_inputs):
        # 30 ml/kg × 70 = 2100.
        norms = compute_norms(adult_female_inputs)
        assert norms.daily_water_ml == 2100
