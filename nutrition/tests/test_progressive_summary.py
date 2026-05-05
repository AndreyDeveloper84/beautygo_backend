"""DRF-266 — 14d/28d weekly progressive summary view.

Track E retention play: power users (commitment_days >= 14) unlock a
two-week summary; commitment >= 28 unlocks a four-week summary with
trends. The 7-day variant remains the default (backwards-compat for
existing callers).

Endpoint: ``GET /api/v1/nutrition/internal/summary/?period=7|14|28``.
Default ``period=1`` reads today (existing behaviour); ``period`` is a
new parameter that swaps the response shape entirely.

Response (period > 1):
- ``period`` (7/14/28)
- ``unlocked`` (bool) — false when commitment_days < period; bot shows
  a teaser instead of trends
- ``commitment_days`` (int) — current commitment (distinct calendar
  days with ≥1 FoodLog, max 60-day lookback)
- ``trends`` (period > 1, unlocked=true) — per-7-day buckets:
  ``calories_avg_per_week``, ``protein_g_avg_per_week``,
  ``vitamin_d_pct_rda_per_week``, ``omega3_g_avg_per_week``
- ``habits`` — running streaks: breakfast_logged_streak_days,
  water_norm_streak_days, late_dinner_count_in_period
- ``goal_progress`` — only for goal in {lose, gain}; tone/maintain
  return null

Pure-DB aggregation. No LLM. Pattern engine reads same windows in
DRF-262 — keep them in sync.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from nutrition.models import FoodLog, NutritionProfile
from nutrition.services.commitment_service import (
    get_commitment_days,
)
from nutrition.services.nutrition_summary_service import (
    NutritionSummaryService,
)


User = get_user_model()


@pytest.fixture
def progressive_user(db):
    user = User.objects.create_user(
        username="progressive_test", password="x",
        role="client", phone="+79990500001",
    )
    NutritionProfile.objects.create(
        user=user, gender="female", age=40, height_cm=165,
        weight_kg=70.0, goal="lose", pace="moderate",
        daily_kcal=1500, daily_protein_g=110,
    )
    return user


def _seed_food_logs(user, days_back: int, per_day: int = 1, kcal: float = 500):
    """Create N daily FoodLogs going back from now.

    Anchor each log at ``now - d days - i hours`` so every entry is
    strictly in the past. Avoids the off-by-one where seeding "today
    14:00 UTC" while the test machine clock is at 11:00 UTC pushes
    today's row outside ``logged_at__lte=now()`` filters.
    """
    now = datetime.now(timezone.utc)
    for d in range(days_back):
        day_anchor = now - timedelta(days=d)
        for i in range(per_day):
            FoodLog.objects.create(
                user=user, dish_name=f"Test {d}-{i}",
                calories=kcal, protein_g=20, fat_g=10, carbs_g=50,
                meal_type="lunch",
                logged_at=day_anchor - timedelta(hours=i),
            )


@pytest.mark.django_db
class TestCommitmentDays:
    """`get_commitment_days(user)` counts distinct logged calendar days."""

    def test_zero_when_no_logs(self, progressive_user):
        assert get_commitment_days(progressive_user) == 0

    def test_counts_distinct_days(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=10, per_day=3)
        assert get_commitment_days(progressive_user) == 10

    def test_caps_at_60_day_lookback(self, progressive_user):
        # Logs older than 60 days don't count toward commitment.
        _seed_food_logs(progressive_user, days_back=70)
        commitment = get_commitment_days(progressive_user)
        assert commitment <= 60
        assert commitment >= 60  # exactly 60-day window


@pytest.mark.django_db
class TestProgressiveSummaryUnlock:
    """``period=14`` requires commitment_days >= 14; period=28 requires >= 28."""

    def test_period_7_always_unlocked(self, progressive_user):
        # Existing callers — no commitment gate on the default weekly.
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=7,
        )
        assert result.unlocked is True

    def test_period_14_locked_until_commitment_hits(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=10)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        assert result.unlocked is False
        assert result.commitment_days == 10

    def test_period_14_unlocks_at_boundary(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=14)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        assert result.unlocked is True
        assert result.commitment_days == 14

    def test_period_28_locked_until_28_days(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=20)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        assert result.unlocked is False

    def test_period_28_unlocks_at_28_days(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=30)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        assert result.unlocked is True


@pytest.mark.django_db
class TestProgressiveTrends:
    """When unlocked, response carries per-7-day-bucket trends."""

    def test_28d_returns_4_weekly_buckets(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=30, kcal=1500)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        assert result.unlocked is True
        # 4 weeks of trends.
        assert len(result.trends["calories_avg_per_week"]) == 4
        assert len(result.trends["protein_g_avg_per_week"]) == 4

    def test_14d_returns_2_weekly_buckets(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=20, kcal=1200)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        assert result.unlocked is True
        assert len(result.trends["calories_avg_per_week"]) == 2

    def test_calories_average_over_bucket_matches_input(self, progressive_user):
        # 14 days, kcal=1000/day, one log/day → average is 1000.
        _seed_food_logs(progressive_user, days_back=14, kcal=1000)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        # Both weeks should average ~1000 kcal/day (every day fully logged).
        for week_avg in result.trends["calories_avg_per_week"]:
            assert 900 <= week_avg <= 1100

    def test_locked_response_has_no_trends(self, progressive_user):
        # period=14 not unlocked → no trends, just teaser fields.
        _seed_food_logs(progressive_user, days_back=5)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        assert result.unlocked is False
        assert result.trends == {} or result.trends is None


@pytest.mark.django_db
class TestProgressiveGoalProgress:
    """Goal progress block — only for goal in {lose, gain}."""

    def test_lose_goal_shows_progress(self, progressive_user):
        _seed_food_logs(progressive_user, days_back=30)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        # Goal=lose → goal_progress block present (even if empty data).
        assert result.goal_progress is not None
        assert result.goal_progress["type"] == "weight_loss"

    def test_maintain_goal_returns_null(self, progressive_user):
        progressive_user.nutrition_profile.goal = "maintain"
        progressive_user.nutrition_profile.save()
        _seed_food_logs(progressive_user, days_back=30)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        assert result.goal_progress is None

    def test_tone_goal_returns_null(self, progressive_user):
        progressive_user.nutrition_profile.goal = "tone"
        progressive_user.nutrition_profile.save()
        _seed_food_logs(progressive_user, days_back=30)
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=28,
        )
        assert result.goal_progress is None


@pytest.mark.django_db
class TestProgressiveEndpoint:
    """``GET /internal/summary/?period=14|28`` returns ProgressiveSummary."""

    def test_period_param_routes_to_progressive(
        self, progressive_user, settings,
    ):
        settings.NUTRITION_SERVICE_TOKEN = "test-token"
        _seed_food_logs(progressive_user, days_back=20)

        client = APIClient()
        resp = client.get(
            "/api/v1/nutrition/internal/summary/?period=14",
            HTTP_X_SERVICE_TOKEN="test-token",
            HTTP_X_EXTERNAL_USER_ID=f"bot:{progressive_user.id}",
        )
        # The progressive shape carries period + unlocked + commitment_days.
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        assert body.get("period") == 14
        assert "unlocked" in body
        assert "commitment_days" in body

    def test_no_period_param_keeps_legacy_shape(
        self, progressive_user, settings,
    ):
        # Backwards compat: callers without ?period= get the existing
        # /summary/ shape (date / calories_total / ... ).
        settings.NUTRITION_SERVICE_TOKEN = "test-token"
        client = APIClient()
        resp = client.get(
            "/api/v1/nutrition/internal/summary/",
            HTTP_X_SERVICE_TOKEN="test-token",
            HTTP_X_EXTERNAL_USER_ID=f"bot:{progressive_user.id}",
        )
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        # Legacy shape — date/calories_total at top level, no `period`.
        assert "date" in body
        assert "calories_total" in body
        assert "period" not in body

    def test_period_7_uses_progressive_shape(
        self, progressive_user, settings,
    ):
        # ?period=7 explicitly opts into progressive shape (single
        # 1-week bucket). Always unlocked.
        settings.NUTRITION_SERVICE_TOKEN = "test-token"
        _seed_food_logs(progressive_user, days_back=10)

        client = APIClient()
        resp = client.get(
            "/api/v1/nutrition/internal/summary/?period=7",
            HTTP_X_SERVICE_TOKEN="test-token",
            HTTP_X_EXTERNAL_USER_ID=f"bot:{progressive_user.id}",
        )
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        assert body["period"] == 7
        assert body["unlocked"] is True


@pytest.mark.django_db
class TestVitaminDRDAFallback:
    """LB-4 regression: RDA must fall back to settings, not 1 IU."""

    def test_no_profile_rda_field_uses_settings_default(
        self, progressive_user, settings,
    ):
        # Profile lacks daily_vitamin_d_iu (DRF-265 not merged here).
        # Without LB-4 fix, fallback was `or 1` → 20000% percentages.
        settings.NUTRITION_DEFAULT_VITAMIN_D_IU = 600
        # Seed user with 600 IU/day vitamin D × 14 days.
        for d in range(14):
            FoodLog.objects.create(
                user=progressive_user, dish_name=f"d{d}",
                calories=500, protein_g=20, fat_g=10, carbs_g=50,
                meal_type="lunch",
                logged_at=datetime.now(timezone.utc) - timedelta(days=d),
                vitamin_d_iu=600.0,
            )
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        # 600 IU/day vs 600 IU RDA → 100% per week. Without LB-4 fix,
        # this was 60000% (600 / 1 × 100).
        for pct in result.trends["vitamin_d_pct_rda_per_week"]:
            assert 80 <= pct <= 120, (
                f"LB-4: vit D % unexpectedly {pct} — fallback "
                "still not honoring settings default"
            )


@pytest.mark.django_db
class TestLateDinnerTimezoneFix:
    """LB-3 regression: 22:00 MSK dinner must count as late_dinner."""

    def test_msk_22_dinner_counts(self, progressive_user, settings):
        settings.NUTRITION_DEFAULT_VITAMIN_D_IU = 600
        # 22:00 MSK = 19:00 UTC. Old code filtered hour >= 21 UTC,
        # which excluded this. New code filters hour >= 18 UTC (MSK
        # offset 3). 19 >= 18 → counted.
        # Anchor at "yesterday 19:00 UTC" then walk back so all logs
        # are strictly in the past regardless of test-run clock time.
        anchor = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=19, minute=0, second=0, microsecond=0,
        )
        for d in range(14):
            FoodLog.objects.create(
                user=progressive_user, dish_name=f"dinner d{d}",
                calories=500, protein_g=20, fat_g=10, carbs_g=50,
                meal_type="dinner",
                logged_at=anchor - timedelta(days=d),
            )
        # Profile is required by progressive() for unlocked path.
        result = NutritionSummaryService().progressive(
            user=progressive_user, period=14,
        )
        # All 14 days had a 22:00 MSK dinner — count must be > 0.
        assert result.habits["late_dinner_count_in_period"] > 0, (
            "LB-3: 22:00 MSK dinners (= 19:00 UTC) didn't count"
        )
