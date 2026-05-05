"""Shared fixtures + helpers for the Phase 3 smoke suite.

Each fixture mirrors the setup steps the manual checklist asks for so
individual tests stay short and read like the checklist row they cover.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APIClient

from nutrition.models import FoodLog, NutritionProfile, WaterEntry
from users.models import User


SERVICE_TOKEN = "smoke-token-DRF-PHASE-3"
EXTERNAL_USER_ID = "bot:smoke"


@pytest.fixture(autouse=True)
def _smoke_settings(settings):
    """Knobs every test relies on. Mirrors prod env names."""
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_DEFAULT_WATER_GOAL_ML = 2000
    settings.NUTRITION_DEFAULT_PROTEIN_GOAL_G = 100
    settings.NUTRITION_DEFAULT_CALORIES_GOAL = 2000


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client_api():
    return APIClient()


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username=EXTERNAL_USER_ID, role="client", is_proxy=True,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": EXTERNAL_USER_ID,
    }


@pytest.fixture
def seed_beverages(db):
    """One-shot seed via management command — same path prod uses."""
    call_command("seed_beverages")


# ---------------------------------------------------------------------------
# Past-date helpers (§5/§6 — pattern detection + returning success)
# ---------------------------------------------------------------------------


def _today() -> "datetime.date":
    return datetime.now(dt_tz.utc).date()


@pytest.fixture
def add_food_at():
    """Insert a FoodLog at an arbitrary past day with full macros control."""
    def _add(
        user,
        *,
        days_ago: int = 0,
        hour: int = 12,
        kcal: float = 500,
        protein_g: float = 30,
        fat_g: float = 10,
        carbs_g: float = 40,
        dish_name: str = "Test dish",
    ):
        when = datetime.combine(
            _today() - timedelta(days=days_ago),
            datetime.min.time().replace(hour=hour),
            tzinfo=dt_tz.utc,
        )
        return FoodLog.objects.create(
            user=user,
            dish_name=dish_name,
            portion_multiplier=1.0,
            calories=kcal,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            meal_type="lunch",
            logged_at=when,
            idempotency_key=str(uuid4()),
        )
    return _add


@pytest.fixture
def add_water_at():
    """Insert a WaterEntry at an arbitrary past day."""
    def _add(
        user,
        *,
        days_ago: int = 0,
        hour: int = 10,
        ml: int = 250,
        beverage=None,
        caffeine_mg: float = 0.0,
    ):
        when = datetime.combine(
            _today() - timedelta(days=days_ago),
            datetime.min.time().replace(hour=hour),
            tzinfo=dt_tz.utc,
        )
        return WaterEntry.objects.create(
            user=user,
            beverage=beverage,
            ts=when,
            ml=ml,
            water_ml=float(ml) if beverage is None else float(ml) * beverage.water_coefficient,
            caffeine_mg=caffeine_mg,
        )
    return _add


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_profile(proxy_user):
    """Create a NutritionProfile bypassing the upsert pipeline.

    Useful when the test wants to control health_flags / norms without
    going through the validation endpoint.
    """
    def _make(**overrides) -> NutritionProfile:
        defaults = {
            "user": proxy_user,
            "gender": "female",
            "age": 40,
            "height_cm": 165,
            "weight_kg": 70.0,
            "activity_coefficient": 1.4,
            "goal": "maintain",
            "pace": "moderate",
            "daily_kcal": 2000,
            "daily_protein_g": 100,
            "daily_water_ml": 2000,
        }
        defaults.update(overrides)
        return NutritionProfile.objects.create(**defaults)
    return _make
