"""Tests for water + beauty-insight retention beat tasks (Slice N4).

Both tasks are idempotent — re-running on the same window finds
already-sent Notification rows and skips. Tests prove that.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest

from notifications.models import Notification
from notifications.tasks import (
    dispatch_beauty_insights,
    dispatch_water_reminders,
)
from nutrition.models import FoodLog, WaterLog
from users.models import User


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="ret-client", password="x", role="client",
        phone="+79997770000",
    )


@pytest.fixture
def other_active_client(db):
    return User.objects.create_user(
        username="ret-other", password="x", role="client",
        phone="+79997770001",
    )


# Tests run with deliver_notification.delay patched so we don't need a
# Celery worker; assertions look at persisted Notification rows.
@pytest.fixture(autouse=True)
def _no_celery_dispatch():
    with patch("notifications.tasks.deliver_notification.delay"):
        yield


def _now_utc() -> datetime:
    return datetime.now(dt_tz.utc)


def _today_start() -> datetime:
    return datetime.combine(
        _now_utc().date(), datetime.min.time(), tzinfo=dt_tz.utc,
    )


# ---------------------------------------------------------------------------
# Water reminders
# ---------------------------------------------------------------------------


class TestDispatchWaterReminders:
    def test_no_active_users_returns_zero(self, db):
        assert dispatch_water_reminders() == {"queued": 0, "skipped": 0}

    def test_user_behind_goal_gets_reminder(self, client_user):
        # Active recently + below half-goal today.
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        assert result["queued"] == 1
        assert Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).count() == 1

    def test_user_at_goal_skipped(self, client_user):
        # Already past 50% threshold (default 1000ml at goal=2000).
        WaterLog.objects.create(
            user=client_user, amount_ml=500, logged_at=_now_utc(),
        )
        WaterLog.objects.create(
            user=client_user, amount_ml=500, logged_at=_now_utc(),
        )
        WaterLog.objects.create(
            user=client_user, amount_ml=200, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        assert result["queued"] == 0
        assert result["skipped"] == 1

    def test_idempotent_within_today(self, client_user):
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        dispatch_water_reminders()
        result = dispatch_water_reminders()
        assert result["queued"] == 0
        assert result["skipped"] == 1
        # Only one Notification persisted across the two beats.
        assert Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).count() == 1

    def test_dormant_users_excluded(self, db, client_user):
        # WaterLog 30 days ago — outside the 7-day active window.
        old = _now_utc() - timedelta(days=30)
        WaterLog.objects.create(user=client_user, amount_ml=250, logged_at=old)
        result = dispatch_water_reminders()
        assert result == {"queued": 0, "skipped": 0}

    def test_other_users_water_only_counts_for_themselves(
        self, client_user, other_active_client,
    ):
        # client_user is at 250 (behind), other user is past goal.
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        WaterLog.objects.create(
            user=other_active_client, amount_ml=2200, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        assert result["queued"] == 1
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Beauty insights
# ---------------------------------------------------------------------------


class TestDispatchBeautyInsights:
    def test_no_active_users_returns_zero(self, db):
        assert dispatch_beauty_insights()["queued"] == 0

    def test_active_user_gets_insight(self, client_user):
        # Active = at least one FoodLog in the last 7 days.
        FoodLog.objects.create(
            user=client_user, dish_name="борщ",
            calories=147, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
            meal_type="lunch", logged_at=_now_utc(),
        )
        result = dispatch_beauty_insights()
        assert result["queued"] == 1
        n = Notification.objects.get(
            user=client_user, template_id="beauty_insight",
        )
        assert "1 приёмов пищи" in n.body or "приём" in n.body

    def test_idempotent_within_week(self, client_user):
        FoodLog.objects.create(
            user=client_user, dish_name="x",
            calories=100, protein_g=1, fat_g=1, carbs_g=10,
            meal_type="lunch", logged_at=_now_utc(),
        )
        dispatch_beauty_insights()
        result = dispatch_beauty_insights()
        assert result["queued"] == 0
        assert result["skipped"] == 1

    def test_user_cap_respected(self, db, settings):
        # 5 active users, cap=2 → only 2 receive insights this tick.
        settings.BEAUTY_INSIGHT_USER_CAP = 2
        users = []
        for i in range(5):
            u = User.objects.create_user(
                username=f"cap-{i}", password="x", role="client",
                phone=f"+7999777{1000+i:04d}",
            )
            FoodLog.objects.create(
                user=u, dish_name="x",
                calories=100, protein_g=1, fat_g=1, carbs_g=10,
                meal_type="lunch", logged_at=_now_utc(),
            )
            users.append(u)
        result = dispatch_beauty_insights()
        assert result["queued"] == 2

    def test_dormant_user_excluded(self, client_user):
        # FoodLog 14 days ago — outside the 7-day active window.
        old = _now_utc() - timedelta(days=14)
        FoodLog.objects.create(
            user=client_user, dish_name="x",
            calories=100, protein_g=1, fat_g=1, carbs_g=10,
            meal_type="lunch", logged_at=old,
        )
        result = dispatch_beauty_insights()
        assert result["queued"] == 0

    def test_failed_insight_build_doesnt_break_cohort(
        self, client_user, other_active_client,
    ):
        # First user's _build_insight_text raises; second still gets a
        # notification so one bad LLM call doesn't drop the whole tick.
        for u in (client_user, other_active_client):
            FoodLog.objects.create(
                user=u, dish_name="x",
                calories=100, protein_g=1, fat_g=1, carbs_g=10,
                meal_type="lunch", logged_at=_now_utc(),
            )

        original = "notifications.tasks._build_insight_text"
        side_effects = iter([RuntimeError("LLM down"), "ok-text"])

        def flaky(_user):
            v = next(side_effects)
            if isinstance(v, Exception):
                raise v
            return v

        with patch(original, flaky):
            result = dispatch_beauty_insights()

        assert result["queued"] == 1
        assert result["failed"] == 1
