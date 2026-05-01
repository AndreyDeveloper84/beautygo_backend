"""Unit tests for NutritionSummaryService.weekly_deficits + InternalDeficitsView (DRF-248)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog
from nutrition.services.deficit_hints import build_deficit_hint
from nutrition.services.nutrition_summary_service import (
    NutritionSummaryService,
    WeeklyDeficits,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-deficits-token"
DEFICITS_URL = "/api/v1/nutrition/internal/deficits/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_DEFAULT_PROTEIN_GOAL_G = 80
    settings.FOOD_DEFICIT_PROTEIN_THRESHOLD_PCT = 0.6
    settings.FOOD_DEFICIT_MIN_STREAK_DAYS = 3


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:248", role="client", is_proxy=True,
    )


def _log_meal(user, *, day_offset: int, protein_g: float) -> None:
    """Create a FoodLog `day_offset` UTC days before today."""
    when = datetime.now(dt_tz.utc) - timedelta(days=day_offset)
    FoodLog.objects.create(
        user=user,
        dish_name="x",
        portion_multiplier=1.0,
        calories=200,
        protein_g=protein_g,
        fat_g=5,
        carbs_g=20,
        meal_type="lunch",
        logged_at=when,
    )


# ---------------------------------------------------------------------------
# Service-level
# ---------------------------------------------------------------------------


class TestWeeklyDeficitsService:
    def test_no_logs_returns_empty(self, proxy_user):
        d = NutritionSummaryService().weekly_deficits(user_id=proxy_user.id)
        assert d.days_observed == 0
        assert d.protein_avg_pct_goal is None
        assert d.protein_low_streak_days == 0

    def test_streak_counts_consecutive_low_days_to_today(self, proxy_user):
        # Goal=80, threshold 0.6 → low if <48g.
        # Days -3, -2, -1, 0 all at 30g → streak = 4
        for offset in (3, 2, 1, 0):
            _log_meal(proxy_user, day_offset=offset, protein_g=30)
        d = NutritionSummaryService().weekly_deficits(user_id=proxy_user.id)
        assert d.days_observed == 4
        assert d.protein_low_streak_days == 4
        # All days below threshold → avg pct ≈ 30/80 = 0.375
        assert 0.36 < d.protein_avg_pct_goal < 0.39

    def test_streak_breaks_on_high_day(self, proxy_user):
        _log_meal(proxy_user, day_offset=3, protein_g=30)
        _log_meal(proxy_user, day_offset=2, protein_g=80)  # at goal — breaks streak
        _log_meal(proxy_user, day_offset=1, protein_g=30)
        _log_meal(proxy_user, day_offset=0, protein_g=30)
        d = NutritionSummaryService().weekly_deficits(user_id=proxy_user.id)
        assert d.days_observed == 4
        # Trailing streak: today + yesterday = 2 (day -2 is at goal, breaks)
        assert d.protein_low_streak_days == 2

    def test_streak_breaks_on_missing_day(self, proxy_user):
        # Day -1 has no log → streak ends at today only.
        _log_meal(proxy_user, day_offset=2, protein_g=30)
        _log_meal(proxy_user, day_offset=0, protein_g=30)
        d = NutritionSummaryService().weekly_deficits(user_id=proxy_user.id)
        assert d.days_observed == 2
        assert d.protein_low_streak_days == 1


# ---------------------------------------------------------------------------
# Hint mapping
# ---------------------------------------------------------------------------


class TestBuildDeficitHint:
    def test_below_threshold_streak_no_hint(self, settings):
        settings.FOOD_DEFICIT_MIN_STREAK_DAYS = 3
        result = build_deficit_hint(WeeklyDeficits(
            days_observed=2,
            protein_avg_pct_goal=0.5,
            protein_low_streak_days=2,  # < 3
        ))
        assert result.hint == ""
        assert result.fired_keys == []

    def test_at_threshold_streak_fires(self, settings):
        settings.FOOD_DEFICIT_MIN_STREAK_DAYS = 3
        result = build_deficit_hint(WeeklyDeficits(
            days_observed=4,
            protein_avg_pct_goal=0.4,
            protein_low_streak_days=3,
        ))
        assert "protein_low_streak" in result.fired_keys
        assert "3 дн" in result.hint

    def test_zero_observed_returns_empty(self, settings):
        settings.FOOD_DEFICIT_MIN_STREAK_DAYS = 3
        result = build_deficit_hint(WeeklyDeficits(
            days_observed=0,
            protein_avg_pct_goal=None,
            protein_low_streak_days=0,
        ))
        assert result.hint == ""


# ---------------------------------------------------------------------------
# InternalDeficitsView
# ---------------------------------------------------------------------------


class TestInternalDeficitsView:
    def test_auth_missing_token_401(self):
        c = APIClient()
        resp = c.get(DEFICITS_URL, HTTP_X_EXTERNAL_USER_ID="bot:248")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_returns_zero_signal_for_empty_user(self, proxy_user):
        c = APIClient()
        resp = c.get(
            DEFICITS_URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:248",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["days_observed"] == 0
        assert body["protein_avg_pct_goal"] is None
        assert body["protein_low_streak_days"] == 0
        assert body["hint"] == ""
        assert body["fired_keys"] == []

    def test_returns_hint_when_streak_triggers(self, proxy_user):
        for offset in (3, 2, 1, 0):
            _log_meal(proxy_user, day_offset=offset, protein_g=30)
        c = APIClient()
        resp = c.get(
            DEFICITS_URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:248",
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["protein_low_streak_days"] == 4
        assert body["fired_keys"] == ["protein_low_streak"]
        assert "белок" in body["hint"].lower()

    def test_invalid_days_param_returns_400(self, proxy_user):
        c = APIClient()
        resp = c.get(
            DEFICITS_URL + "?days=99",
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:248",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_protein_goal_skips_hint_safely(self, proxy_user, settings):
        settings.NUTRITION_DEFAULT_PROTEIN_GOAL_G = 0
        for offset in (3, 2, 1, 0):
            _log_meal(proxy_user, day_offset=offset, protein_g=30)
        c = APIClient()
        resp = c.get(
            DEFICITS_URL,
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID="bot:248",
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["protein_avg_pct_goal"] is None
        assert body["protein_low_streak_days"] == 0
        assert body["hint"] == ""
