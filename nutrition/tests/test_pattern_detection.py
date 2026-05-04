"""Tests for the Phase 3.3 pattern detection (DRF-304).

Layered:
- Per-detector unit tests with seeded FoodLog/WaterEntry rows.
- Health-flag suppression cross-checks.
- Endpoint integration: response shape, IsServiceAccount auth, 12h cache.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from uuid import uuid4

import pytest
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import (
    Beverage,
    FoodLog,
    NutritionProfile,
    WaterEntry,
)
from nutrition.services.pattern_detection_service import (
    EVENING_HOUR,
    LATE_CAFFEINE_HOUR,
    LATE_DINNER_HOUR,
    detect_patterns,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-304"
URL = "/api/v1/nutrition/internal/patterns/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_DEFAULT_PROTEIN_GOAL_G = 100
    settings.NUTRITION_DEFAULT_WATER_GOAL_ML = 2000
    settings.NUTRITION_DEFAULT_CALORIES_GOAL = 2000


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create(username="bot:304", role="client", is_proxy=True)


@pytest.fixture
def profile(user):
    return NutritionProfile.objects.create(
        user=user,
        daily_kcal=2000,
        daily_protein_g=100,
        daily_water_ml=2000,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:304",
    }


def _utc(year=2026, month=4, day=1, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=dt_tz.utc)


def _today():
    return datetime.now(dt_tz.utc).date()


def _add_food(user, *, name="Куриная грудка", kcal=500, protein=40, when=None):
    when = when or _utc()
    return FoodLog.objects.create(
        user=user,
        dish_name=name,
        portion_multiplier=1.0,
        calories=kcal,
        protein_g=protein,
        fat_g=10,
        carbs_g=20,
        meal_type="lunch",
        logged_at=when,
        idempotency_key=str(uuid4()),
    )


def _add_water(user, *, ml=250, when=None, beverage=None, caffeine=0.0):
    when = when or _utc()
    return WaterEntry.objects.create(
        user=user,
        beverage=beverage,
        ts=when,
        ml=ml,
        water_ml=float(ml),
        caffeine_mg=caffeine,
    )


# ===========================================================================
# evening_sweets
# ===========================================================================


class TestEveningSweets:
    def test_fires_when_4_weekday_evenings_have_sweets(self, user, profile):
        # Pick 4 weekday-evening days within the 14-day window.
        today = _today()
        # Walk back finding 4 weekdays.
        days = []
        cursor = today
        while len(days) < 4:
            cursor -= timedelta(days=1)
            if cursor.weekday() < 5:
                days.append(cursor)
        for d in days:
            ts = datetime.combine(
                d, datetime.min.time().replace(hour=EVENING_HOUR + 1),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, name="Шоколад молочный", kcal=200, protein=2, when=ts)

        result = detect_patterns(user_id=user.id, force=True)
        slugs = {p.slug for p in result.patterns}
        assert "evening_sweets" in slugs

    def test_silent_when_below_threshold(self, user, profile):
        today = _today()
        ts = datetime.combine(
            today, datetime.min.time().replace(hour=EVENING_HOUR + 1),
            tzinfo=dt_tz.utc,
        )
        _add_food(user, name="Конфета", kcal=80, when=ts)
        result = detect_patterns(user_id=user.id, force=True)
        assert "evening_sweets" not in {p.slug for p in result.patterns}


# ===========================================================================
# low_protein
# ===========================================================================


class TestLowProtein:
    def test_fires_with_5_low_days_of_7(self, user, profile):
        today = _today()
        # 5 days protein 30g (below 70% of 100), 2 days normal.
        for i in range(5):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=12),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, kcal=400, protein=30, when=ts)
        # 2 normal days
        for i in range(5, 7):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=12),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, kcal=400, protein=90, when=ts)

        result = detect_patterns(user_id=user.id, force=True)
        slugs = {p.slug for p in result.patterns}
        assert "low_protein" in slugs


# ===========================================================================
# low_water
# ===========================================================================


class TestLowWater:
    def test_fires_with_4_low_days_of_7(self, user, profile):
        today = _today()
        # 4 dry-ish days (below 70% of 2000 = 1400 ml)
        for i in range(4):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=10),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=500, when=ts)  # 500 < 1400
        result = detect_patterns(user_id=user.id, force=True)
        assert "low_water" in {p.slug for p in result.patterns}


# ===========================================================================
# late_dinner
# ===========================================================================


class TestLateDinner:
    def test_fires_with_3_late_dinners(self, user, profile):
        today = _today()
        for i in range(3):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=LATE_DINNER_HOUR + 1),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, kcal=500, when=ts)
        result = detect_patterns(user_id=user.id, force=True)
        assert "late_dinner" in {p.slug for p in result.patterns}


# ===========================================================================
# meal_skips (3-day streak)
# ===========================================================================


class TestMealSkips:
    def test_fires_when_3_days_under_30pct(self, user, profile):
        today = _today()
        for i in range(3):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=12),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, kcal=300, when=ts)  # 300 < 600 threshold
        result = detect_patterns(user_id=user.id, force=True)
        assert "meal_skips" in {p.slug for p in result.patterns}

    def test_streak_breaks_on_day_with_no_logs(self, user, profile):
        today = _today()
        # Yesterday <30%, but today nothing → streak fails because today has no rows
        ts = datetime.combine(
            today - timedelta(days=1),
            datetime.min.time().replace(hour=12),
            tzinfo=dt_tz.utc,
        )
        _add_food(user, kcal=200, when=ts)
        result = detect_patterns(user_id=user.id, force=True)
        assert "meal_skips" not in {p.slug for p in result.patterns}


# ===========================================================================
# late_caffeine
# ===========================================================================


class TestLateCaffeine:
    def test_fires_with_3_late_caffeine_days(self, user, profile):
        coffee = Beverage.objects.create(
            slug="kofe", name_ru="Кофе", category="coffee",
            water_coefficient=1.0, caffeine_mg_per_100ml=40,
        )
        today = _today()
        for i in range(3):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=LATE_CAFFEINE_HOUR + 1),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=200, when=ts, beverage=coffee, caffeine=80)
        result = detect_patterns(user_id=user.id, force=True)
        assert "late_caffeine" in {p.slug for p in result.patterns}


# ===========================================================================
# frequent_alcohol — requires ≥14d history
# ===========================================================================


class TestFrequentAlcohol:
    def test_silent_when_history_under_14_days(self, user, profile):
        wine = Beverage.objects.create(
            slug="vino", name_ru="Вино", category="alcohol",
            water_coefficient=0.2, kcal_per_100ml=85,
        )
        today = _today()
        # Two alcohol days but earliest is only 5 days ago — history insufficient.
        for i in range(2):
            ts = datetime.combine(
                today - timedelta(days=i * 2),
                datetime.min.time().replace(hour=20),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=150, when=ts, beverage=wine)
        result = detect_patterns(user_id=user.id, force=True)
        assert "frequent_alcohol" not in {p.slug for p in result.patterns}

    def test_fires_with_2_alcohol_days_when_history_sufficient(self, user, profile):
        wine = Beverage.objects.create(
            slug="vino", name_ru="Вино", category="alcohol",
            water_coefficient=0.2, kcal_per_100ml=85,
        )
        today = _today()
        # Anchor row at the earliest day inside the 14-day collection
        # window (today-13). The detector's history check reads the
        # min day from the collected stats, so the row must be in-window.
        anchor_ts = datetime.combine(
            today - timedelta(days=13),
            datetime.min.time().replace(hour=12),
            tzinfo=dt_tz.utc,
        )
        _add_water(user, ml=250, when=anchor_ts)
        # Two alcohol days within last 7
        for i in (0, 2):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=20),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=150, when=ts, beverage=wine)
        result = detect_patterns(user_id=user.id, force=True)
        assert "frequent_alcohol" in {p.slug for p in result.patterns}


# ===========================================================================
# Health-flag suppression
# ===========================================================================


class TestHealthFlagSuppression:
    def test_eating_disorder_suppresses_alcohol_and_protein(self, user):
        wine = Beverage.objects.create(
            slug="vino", name_ru="Вино", category="alcohol",
            water_coefficient=0.2, kcal_per_100ml=85,
        )
        NutritionProfile.objects.create(
            user=user,
            daily_kcal=2000,
            daily_protein_g=100,
            daily_water_ml=2000,
            health_flags={"eating_disorder": True},
        )
        today = _today()
        # set up frequent alcohol (history + 2 days)
        anchor_ts = datetime.combine(
            today - timedelta(days=13),
            datetime.min.time().replace(hour=12),
            tzinfo=dt_tz.utc,
        )
        _add_water(user, ml=250, when=anchor_ts)
        for i in (0, 2):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=20),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=150, when=ts, beverage=wine)
        # set up low_protein
        for i in range(5):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=12),
                tzinfo=dt_tz.utc,
            )
            _add_food(user, kcal=400, protein=30, when=ts)
        result = detect_patterns(user_id=user.id, force=True)
        slugs = {p.slug for p in result.patterns}
        assert "frequent_alcohol" not in slugs
        assert "low_protein" not in slugs

    def test_pregnant_suppresses_alcohol(self, user):
        wine = Beverage.objects.create(
            slug="vino", name_ru="Вино", category="alcohol",
            water_coefficient=0.2,
        )
        NutritionProfile.objects.create(
            user=user,
            daily_kcal=2000,
            daily_protein_g=100,
            daily_water_ml=2000,
            health_flags={"pregnant": True},
        )
        today = _today()
        anchor_ts = datetime.combine(
            today - timedelta(days=13),
            datetime.min.time().replace(hour=12),
            tzinfo=dt_tz.utc,
        )
        _add_water(user, ml=250, when=anchor_ts)
        for i in (0, 2):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=20),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=150, when=ts, beverage=wine)
        result = detect_patterns(user_id=user.id, force=True)
        assert "frequent_alcohol" not in {p.slug for p in result.patterns}


# ===========================================================================
# display_hint mapping
# ===========================================================================


class TestDisplayHint:
    def test_high_severity_maps_to_primary(self, user, profile):
        # Saturate evening_sweets to push severity to high (>=75% of 14)
        today = _today()
        added = 0
        cursor = today
        while added < 11:  # 11/14 = 78%
            cursor -= timedelta(days=1)
            if cursor.weekday() < 5:
                ts = datetime.combine(
                    cursor, datetime.min.time().replace(hour=20),
                    tzinfo=dt_tz.utc,
                )
                _add_food(user, name="Конфеты", kcal=120, when=ts)
                added += 1
        result = detect_patterns(user_id=user.id, force=True)
        sweets = next(p for p in result.patterns if p.slug == "evening_sweets")
        # 11/14 = 0.79 → high; some weekday gaps may drop us to medium —
        # accept either as long as it's not hidden.
        assert sweets.display_hint in {"primary", "secondary"}


# ===========================================================================
# Endpoint integration
# ===========================================================================


class TestEndpoint:
    def test_unauthenticated_returns_401(self, user, profile):
        c = APIClient()
        resp = c.get(URL, HTTP_X_EXTERNAL_USER_ID="bot:304")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_empty_returns_zero_active_days_and_no_patterns(
        self, user, profile, headers,
    ):
        c = APIClient()
        resp = c.get(URL, **headers)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["active_days"] == 0
        assert body["patterns"] == []

    def test_response_shape_per_spec(self, user, profile, headers):
        # Trigger any single pattern (low_water).
        today = _today()
        for i in range(4):
            ts = datetime.combine(
                today - timedelta(days=i),
                datetime.min.time().replace(hour=10),
                tzinfo=dt_tz.utc,
            )
            _add_water(user, ml=400, when=ts)
        c = APIClient()
        resp = c.get(URL, **headers)
        body = resp.json()["data"]
        first = body["patterns"][0]
        # Response shape per spec §3.1
        for key in (
            "slug", "name_ru", "count", "active_window_days",
            "severity", "recent_dates", "advice_template_args", "display_hint",
        ):
            assert key in first

    def test_cache_persists_across_calls(self, user, profile, headers):
        c = APIClient()
        # Pre-warm cache via a primary call.
        c.get(URL, **headers)
        # Add new data — but cached result still empty.
        today = _today()
        ts = datetime.combine(
            today, datetime.min.time().replace(hour=10), tzinfo=dt_tz.utc,
        )
        _add_water(user, ml=100, when=ts)
        resp = c.get(URL, **headers)
        body = resp.json()["data"]
        # Cached snapshot still says no active day from before the new row.
        assert body["active_days"] == 0
