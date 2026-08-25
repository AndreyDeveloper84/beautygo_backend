"""DRF-1339: marker of assumed (default-substituted) inputs in norm computation.

When the user skipped weight, ``compute_norms`` silently substitutes
``DEFAULT_WEIGHT_KG`` (nutrition_profile_service.py) and every downstream
number — BMR, kcal, macros, water, the BMR-floor ladder verdict — is
computed from a value the user never stated. This change only makes that
fact visible: a machine-readable ``assumed_inputs`` field in the profile
response plus an ``assumed_input`` entry in the persisted override audit.
No computed number changes.

Layers:
- HTTP: marker present + ``weight_kg`` stays NULL (response and DB) when
  weight is skipped; marker empty when weight is given; a ``bmr_floor``
  verdict on an assumed weight is distinguishable from the same verdict
  on a real weight by the single ``assumed_inputs`` field.
- Snapshot: every computed number frozen from the pre-change code and
  re-asserted here — the invariance proof required by DRF-1339.
"""
from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_profile_service import (
    ProfileInputs,
    compute_norms,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-1339"
URL = "/api/v1/nutrition/internal/profile/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:1339", role="client", is_proxy=True,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:1339",
    }


# ===========================================================================
# HTTP layer — the marker
# ===========================================================================


class TestAssumedInputsMarker:
    def test_no_weight_marks_assumed_and_keeps_weight_null(
        self, proxy_user, headers,
    ):
        c = APIClient()
        resp = c.post(URL, {
            "gender": "female", "age": 40, "height_cm": 165,
            "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        # The marker names the substituted input...
        assert body["assumed_inputs"] == ["weight_kg"]
        # ...the response does not leak the default into weight_kg...
        assert body["weight_kg"] is None
        # ...and the DB column stays NULL — the default is never written.
        profile = NutritionProfile.objects.get(user=proxy_user)
        assert profile.weight_kg is None
        # The persisted audit carries the machine-readable marker.
        assert {
            "reason": "assumed_input", "field": "weight_kg",
        } in profile.last_overrides_applied

        # GET renders the same marker.
        get_resp = c.get(URL, **headers)
        get_body = get_resp.json()["data"]
        assert get_body["assumed_inputs"] == ["weight_kg"]
        assert get_body["weight_kg"] is None

    def test_with_weight_marker_empty(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {
            "gender": "female", "age": 40, "height_cm": 165,
            "weight_kg": 70.0, "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["assumed_inputs"] == []
        assert body["weight_kg"] == 70.0
        profile = NutritionProfile.objects.get(user=proxy_user)
        assert profile.weight_kg == 70.0
        assert not [
            e for e in profile.last_overrides_applied
            if e.get("reason") == "assumed_input"
        ]

    def test_bmr_floor_on_assumed_vs_real_weight_distinguishable(self, db):
        # Both profiles land on goal_overridden_by == "bmr_floor"; only
        # assumed_inputs tells the substituted-weight one apart.
        c = APIClient()
        payload = {
            "gender": "female", "age": 70, "height_cm": 150,
            "activity_coefficient": 1.0, "goal": "lose", "pace": "moderate",
        }

        User.objects.create(username="bot:1339-a", role="client", is_proxy=True)
        resp_assumed = c.post(URL, payload, format="json", **{
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:1339-a",
        })
        body_assumed = resp_assumed.json()["data"]
        assert body_assumed["goal_overridden_by"] == "bmr_floor"
        assert body_assumed["goal"] == "maintain"

        User.objects.create(username="bot:1339-b", role="client", is_proxy=True)
        resp_real = c.post(URL, {**payload, "weight_kg": 45.0}, format="json", **{
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:1339-b",
        })
        body_real = resp_real.json()["data"]
        assert body_real["goal_overridden_by"] == "bmr_floor"
        assert body_real["goal"] == "maintain"

        # Same verdict — distinguished by one field, no text parsing.
        assert body_assumed["assumed_inputs"] == ["weight_kg"]
        assert body_real["assumed_inputs"] == []


# ===========================================================================
# Invariance snapshot — every computed number frozen from the pre-change
# code (captured on origin/dev before DRF-1339). If any number diverges,
# the change is wrong: the marker must show, never recompute.
# ===========================================================================


_SNAPSHOT = {
    "all_known_maintain": {
        "inputs": ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=70.0,
            activity_coefficient=1.4, goal="maintain", pace="moderate",
        ),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238, "daily_water_ml": 2100,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_maintain": {
        "inputs": ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="maintain", pace="moderate",
        ),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238, "daily_water_ml": 2100,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_lose_bmr_floor": {
        "inputs": ProfileInputs(
            gender="female", age=70, height_cm=150, weight_kg=None,
            activity_coefficient=1.0, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 1126, "daily_kcal": 1126, "daily_protein_g": 98,
            "daily_fat_g": 38, "daily_carbs_g": 99, "daily_water_ml": 2100,
            "goal": "maintain", "pace": "gentle",
            "goal_overridden_by": "bmr_floor",
            "daily_vitamin_d_iu": 800, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 8,
            "daily_calcium_mg": 1200, "daily_magnesium_mg": 320,
            "daily_omega3_g": 1.1, "daily_fiber_g": 21,
            "overrides_applied": [
                {"reason": "bmr_floor",
                 "from": {"pace": "moderate"}, "to": {"pace": "gentle"}},
                {"reason": "bmr_floor",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "real_weight_lose_bmr_floor": {
        "inputs": ProfileInputs(
            gender="female", age=70, height_cm=150, weight_kg=45.0,
            activity_coefficient=1.0, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 876, "daily_kcal": 876, "daily_protein_g": 63,
            "daily_fat_g": 29, "daily_carbs_g": 90, "daily_water_ml": 1350,
            "goal": "maintain", "pace": "gentle",
            "goal_overridden_by": "bmr_floor",
            "daily_vitamin_d_iu": 800, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 8,
            "daily_calcium_mg": 1200, "daily_magnesium_mg": 320,
            "daily_omega3_g": 1.1, "daily_fiber_g": 21,
            "overrides_applied": [
                {"reason": "bmr_floor",
                 "from": {"pace": "moderate"}, "to": {"pace": "gentle"}},
                {"reason": "bmr_floor",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "unknown_weight_lose_ok": {
        "inputs": ProfileInputs(
            gender="male", age=30, height_cm=180, weight_kg=None,
            activity_coefficient=1.6, goal="lose", pace="moderate",
        ),
        "expected": {
            "bmr": 1680, "daily_kcal": 2150, "daily_protein_g": 112,
            "daily_fat_g": 72, "daily_carbs_g": 264, "daily_water_ml": 2100,
            "goal": "lose", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 90, "daily_iron_mg": 8,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 400,
            "daily_omega3_g": 1.6, "daily_fiber_g": 38,
            "overrides_applied": [],
        },
    },
    "unknown_weight_all_defaults": {
        "inputs": ProfileInputs(),
        "expected": {
            "bmr": 1370, "daily_kcal": 1918, "daily_protein_g": 98,
            "daily_fat_g": 64, "daily_carbs_g": 238, "daily_water_ml": 2100,
            "goal": "maintain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
    "unknown_weight_pregnant": {
        "inputs": ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="lose", pace="moderate",
            health_flags={"pregnant": True},
        ),
        "expected": {
            "bmr": 1420, "daily_kcal": 2188, "daily_protein_g": 123,
            "daily_fat_g": 73, "daily_carbs_g": 285, "daily_water_ml": 2400,
            "goal": "maintain", "pace": "moderate",
            "goal_overridden_by": "pregnancy",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 85, "daily_iron_mg": 27,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.4, "daily_fiber_g": 28,
            "overrides_applied": [
                {"reason": "pregnancy",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "unknown_weight_ed": {
        "inputs": ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=None,
            activity_coefficient=1.4, goal="lose", pace="moderate",
            health_flags={"eating_disorder": True},
        ),
        "expected": {
            "bmr": 1420, "daily_kcal": 1988, "daily_protein_g": 98,
            "daily_fat_g": 66, "daily_carbs_g": 250, "daily_water_ml": 2100,
            "goal": "maintain", "pace": "moderate",
            "goal_overridden_by": "eating_disorder",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [
                {"reason": "eating_disorder",
                 "from": {"goal": "lose"}, "to": {"goal": "maintain"}},
            ],
        },
    },
    "real_weight_tone": {
        "inputs": ProfileInputs(
            gender="male", age=25, height_cm=175, weight_kg=80.0,
            activity_coefficient=1.5, goal="tone", pace="gentle",
        ),
        "expected": {
            "bmr": 1774, "daily_kcal": 2416, "daily_protein_g": 128,
            "daily_fat_g": 81, "daily_carbs_g": 295, "daily_water_ml": 2400,
            "goal": "tone", "pace": "gentle", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 90, "daily_iron_mg": 8,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 400,
            "daily_omega3_g": 1.6, "daily_fiber_g": 38,
            "overrides_applied": [],
        },
    },
    "unknown_weight_gain": {
        "inputs": ProfileInputs(
            gender="female", age=35, height_cm=170, weight_kg=None,
            activity_coefficient=1.3, goal="gain", pace="moderate",
        ),
        "expected": {
            "bmr": 1426, "daily_kcal": 2040, "daily_protein_g": 98,
            "daily_fat_g": 68, "daily_carbs_g": 259, "daily_water_ml": 2100,
            "goal": "gain", "pace": "moderate", "goal_overridden_by": "",
            "daily_vitamin_d_iu": 600, "daily_vitamin_b12_mcg": 2.4,
            "daily_vitamin_c_mg": 75, "daily_iron_mg": 18,
            "daily_calcium_mg": 1000, "daily_magnesium_mg": 310,
            "daily_omega3_g": 1.1, "daily_fiber_g": 25,
            "overrides_applied": [],
        },
    },
}


class TestComputedNormsSnapshot:
    @pytest.mark.parametrize("case", sorted(_SNAPSHOT))
    def test_norms_unchanged_by_drf1339(self, case):
        norms = compute_norms(_SNAPSHOT[case]["inputs"])
        for field_name, expected in _SNAPSHOT[case]["expected"].items():
            actual = getattr(norms, field_name)
            assert actual == expected, (
                f"{case}.{field_name}: {actual!r} != snapshot {expected!r}"
            )
