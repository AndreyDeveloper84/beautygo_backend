"""Tests for the returning_success insight (DRF-305).

Detector + endpoint coverage. Trigger condition: 3+ failure days
(<60% of kcal goal) followed by 2+ recovery days (80-110% of goal)
ending today. Eating-disorder always returns detected=false.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from uuid import uuid4

import pytest
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, NutritionProfile
from nutrition.services.returning_success_service import (
    detect_returning_success,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-305"
URL = "/api/v1/nutrition/internal/insights/returning_success/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_DEFAULT_CALORIES_GOAL = 2000


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create(username="bot:305", role="client", is_proxy=True)


@pytest.fixture
def profile(user):
    return NutritionProfile.objects.create(user=user, daily_kcal=2000)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:305",
    }


def _today():
    return datetime.now(dt_tz.utc).date()


def _seed_day(user, *, days_ago: int, kcal: float):
    """Insert a FoodLog row that lands on the day ``days_ago`` ago (UTC)."""
    when = datetime.combine(
        _today() - timedelta(days=days_ago),
        datetime.min.time().replace(hour=12),
        tzinfo=dt_tz.utc,
    )
    FoodLog.objects.create(
        user=user,
        dish_name="x",
        portion_multiplier=1.0,
        calories=kcal,
        protein_g=0,
        fat_g=0,
        carbs_g=0,
        meal_type="lunch",
        logged_at=when,
        idempotency_key=str(uuid4()),
    )


# ===========================================================================
# Happy path
# ===========================================================================


class TestHappyPath:
    def test_three_fails_then_two_recovery_detects(self, user, profile):
        # Layout (today is day 0):
        # day 4..2: failure (<60% = <1200)
        # day 1, 0: recovery (80-110% = 1600..2200)
        for i in (4, 3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)

        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is True
        assert result.failure_streak_days == 3
        assert result.recovery_days == 2
        assert result.since_recovery_started == (
            (_today() - timedelta(days=1)).isoformat()
        )

    def test_longer_streaks_count_correctly(self, user, profile):
        for i in (6, 5, 4, 3, 2):
            _seed_day(user, days_ago=i, kcal=900)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=2000)

        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is True
        assert result.failure_streak_days == 5
        assert result.recovery_days == 2


# ===========================================================================
# Negative cases
# ===========================================================================


class TestNoSignal:
    def test_no_logs_returns_false(self, user, profile):
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False

    def test_only_recovery_no_prior_failure(self, user, profile):
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False
        assert result.recovery_days == 2

    def test_only_one_recovery_day(self, user, profile):
        for i in (4, 3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        _seed_day(user, days_ago=0, kcal=1900)
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False

    def test_failure_streak_too_short(self, user, profile):
        # Only 2 failure days then recovery — below MIN_FAILURE_STREAK=3.
        for i in (3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False
        assert result.failure_streak_days == 2

    def test_interrupted_recovery(self, user, profile):
        # 3 failure days then ABOVE recovery range yesterday → recovery
        # streak = 0 today (today is recovery, yesterday is "other"), so
        # recovery streak from today is 1 (only today). Below min.
        for i in (4, 3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        _seed_day(user, days_ago=1, kcal=2500)  # above 110% — "other"
        _seed_day(user, days_ago=0, kcal=1900)  # recovery today
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False

    def test_gap_day_breaks_failure_streak(self, user, profile):
        # 2 failure days, no log day, then 1 more failure, then recovery.
        # Failure streak walking back from day-3 stops at the gap → streak=1.
        _seed_day(user, days_ago=3, kcal=800)
        # day 2 has no log → counts as gap, breaks streak when walking back
        _seed_day(user, days_ago=4, kcal=800)
        _seed_day(user, days_ago=5, kcal=800)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False


# ===========================================================================
# Eating disorder mode
# ===========================================================================


class TestEatingDisorderMode:
    def test_eating_disorder_always_returns_false(self, user):
        NutritionProfile.objects.create(
            user=user, daily_kcal=2000,
            health_flags={"eating_disorder": True},
        )
        # Set up a real returning success — must still return false.
        for i in (4, 3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)
        result = detect_returning_success(user_id=user.id, force=True)
        assert result.detected is False
        assert result.failure_streak_days == 0
        assert result.recovery_days == 0


# ===========================================================================
# Endpoint integration
# ===========================================================================


class TestEndpoint:
    def test_unauthenticated_returns_401(self, user, profile):
        c = APIClient()
        resp = c.get(URL, HTTP_X_EXTERNAL_USER_ID="bot:305")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_invalid_external_id_returns_400(self):
        c = APIClient()
        resp = c.get(
            URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="no-colon",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_response_envelope_when_detected(self, user, profile, headers):
        for i in (4, 3, 2):
            _seed_day(user, days_ago=i, kcal=800)
        for i in (1, 0):
            _seed_day(user, days_ago=i, kcal=1900)
        c = APIClient()
        resp = c.get(URL, **headers)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["detected"] is True
        assert body["failure_streak_days"] == 3
        assert body["recovery_days"] == 2
        assert body["since_recovery_started"] is not None

    def test_response_envelope_when_not_detected(self, user, profile, headers):
        c = APIClient()
        resp = c.get(URL, **headers)
        body = resp.json()["data"]
        assert body["detected"] is False
