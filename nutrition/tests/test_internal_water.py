"""Integration tests for /api/v1/nutrition/internal/water/* (DRF-302).

Covers the WaterEntry create/delete/restore/today flows including the
acceptance criteria from docs/plans/maxbot-phase3-linear-issues.md:
milestone idempotency per-day per-threshold, alcohol hint, caffeine
warning under pregnancy, eating-disorder mode field stripping, soft-
delete + 15-minute restore window, ml validation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, WaterEntry
from nutrition.services.water_entry_service import (
    NutritionContext,
    purge_deleted_water_entries,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-302"
WATER_URL = "/api/v1/nutrition/internal/water/"
TODAY_URL = "/api/v1/nutrition/internal/water/today/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_DEFAULT_WATER_GOAL_ML = 2000


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:302", role="client", is_proxy=True,
    )


@pytest.fixture
def seed(db):
    call_command("seed_beverages")


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:302",
    }


def _post_water(c: APIClient, body: dict, headers: dict, idem: str | None = None):
    h = dict(headers)
    if idem:
        h["HTTP_IDEMPOTENCY_KEY"] = idem
    return c.post(WATER_URL, body, format="json", **h)


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


class TestAuthAndValidation:
    def test_missing_service_token_returns_401(self, proxy_user):
        c = APIClient()
        resp = c.post(
            WATER_URL,
            {"ml": 250},
            format="json",
            HTTP_X_EXTERNAL_USER_ID="bot:302",
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_ml_below_min_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 5}, headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_ml_above_max_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 5000}, headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_beverage_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(
            c, {"ml": 250, "beverage_slug": "doesnotexist"}, headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Basic create
# ---------------------------------------------------------------------------


class TestCreateWater:
    def test_pure_water_no_beverage(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250}, headers)
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert body["kcal"] == 0
        assert body["beverage_name"] is None
        assert body["today_total_water_ml"] == 250
        assert body["today_norm_water_ml"] == 2000
        assert body["today_progress_pct"] == 12

    def test_coffee_applies_macros(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(
            c, {"ml": 200, "beverage_slug": "kofe_chernyi"}, headers,
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert body["beverage_name"] == "Чёрный кофе"
        assert body["beverage_label"] == "чашка"
        assert body["water_ml"] == 200  # water_coefficient 1.0
        assert body["caffeine_mg"] == 80.0  # 200ml × 40 mg/100ml

    def test_kcal_creates_food_log_mirror(self, proxy_user, seed, headers):
        """Beverages with kcal>0 mirror into FoodLog so /summary/ stays accurate."""
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        assert resp.status_code == status.HTTP_201_CREATED
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.food_log_id is not None
        assert FoodLog.objects.filter(id=entry.food_log_id).exists()

    def test_zero_kcal_skips_food_log_mirror(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250}, headers)
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.food_log_id is None


# ---------------------------------------------------------------------------
# Idempotency-Key
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_replay_returns_same_entry(self, proxy_user, seed, headers):
        c = APIClient()
        body = {"ml": 250}
        r1 = _post_water(c, body, headers, idem="abc-123")
        r2 = _post_water(c, body, headers, idem="abc-123")
        assert r1.status_code == r2.status_code == status.HTTP_201_CREATED
        assert r1.json()["data"]["entry_id"] == r2.json()["data"]["entry_id"]
        assert WaterEntry.objects.count() == 1


# ---------------------------------------------------------------------------
# Milestone idempotency (per-day per-threshold)
# ---------------------------------------------------------------------------


class TestMilestoneIdempotency:
    def test_50pct_fires_once(self, proxy_user, seed, headers):
        c = APIClient()
        # Two 500ml drinks → 1000ml = 50% of 2000ml goal
        r1 = _post_water(c, {"ml": 500}, headers)
        assert r1.json()["data"]["milestone_text"] is None
        r2 = _post_water(c, {"ml": 500}, headers)
        assert "Половина" in (r2.json()["data"]["milestone_text"] or "")
        # Add another bracket-crossing entry — should NOT re-fire 50%.
        r3 = _post_water(c, {"ml": 200}, headers)
        body3 = r3.json()["data"]
        assert body3["milestone_text"] is None or "Половина" not in body3["milestone_text"]

    def test_each_threshold_fires_independently(self, proxy_user, seed, headers):
        c = APIClient()
        # One large drink crossing 50% boundary
        _post_water(c, {"ml": 1100}, headers)  # 1100 / 2000 = 55%
        # Push to 100%
        r2 = _post_water(c, {"ml": 950}, headers)  # total 2050
        assert "норма выполнена" in (r2.json()["data"]["milestone_text"] or "").lower()
        # And to 150%
        r3 = _post_water(c, {"ml": 1000}, headers)  # total 3050 = 152%
        assert "запасом" in (r3.json()["data"]["milestone_text"] or "")

    def test_undo_does_not_unlock_milestone(self, proxy_user, seed, headers):
        """Once user saw 50% message, undoing the entry shouldn't re-arm it."""
        c = APIClient()
        _post_water(c, {"ml": 500}, headers)
        r2 = _post_water(c, {"ml": 500}, headers)
        entry_id = r2.json()["data"]["entry_id"]
        assert "Половина" in (r2.json()["data"]["milestone_text"] or "")
        # Undo the milestone-firing entry
        del_resp = c.delete(
            f"{WATER_URL}{entry_id}/", **headers,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        # Re-add water — must NOT re-fire 50%
        r3 = _post_water(c, {"ml": 500}, headers)
        assert r3.json()["data"]["milestone_text"] is None


# ---------------------------------------------------------------------------
# Alcohol hint
# ---------------------------------------------------------------------------


class TestAlcoholHint:
    def test_alcohol_sets_hint_flag(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 150, "beverage_slug": "vino_krasnoe"}, headers)
        body = resp.json()["data"]
        assert body["alcohol_recovery_hint"] is True

    def test_non_alcohol_no_hint(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250, "beverage_slug": "voda"}, headers)
        body = resp.json()["data"]
        assert body["alcohol_recovery_hint"] is False


# ---------------------------------------------------------------------------
# Caffeine warning (pregnant)
# ---------------------------------------------------------------------------


class TestCaffeineWarning:
    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(pregnant=True, daily_water_ml=2000),
    )
    def test_pregnant_over_threshold_warns(self, _ctx, proxy_user, seed, headers):
        c = APIClient()
        # 600 ml espresso × 180 mg/100ml = 1080 mg — well over 200 mg
        resp = _post_water(c, {"ml": 200, "beverage_slug": "espresso"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is not None

    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(pregnant=True, daily_water_ml=2000),
    )
    def test_pregnant_under_threshold_no_warning(
        self, _ctx, proxy_user, seed, headers,
    ):
        c = APIClient()
        # 100 ml black coffee × 40 mg/100ml = 40 mg — below threshold
        resp = _post_water(c, {"ml": 100, "beverage_slug": "kofe_chernyi"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is None

    def test_non_pregnant_default_no_warning(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "espresso"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is None


# ---------------------------------------------------------------------------
# Eating disorder mode
# ---------------------------------------------------------------------------


class TestEatingDisorderMode:
    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(eating_disorder=True, daily_water_ml=2000),
    )
    def test_strips_kcal_milestone_alcohol_hint(
        self, _ctx, proxy_user, seed, headers,
    ):
        c = APIClient()
        # Big drink that would normally fire 50% milestone + alcohol hint
        resp = _post_water(c, {"ml": 1500, "beverage_slug": "vino_krasnoe"}, headers)
        body = resp.json()["data"]
        assert body["kcal"] == 0
        assert body["protein_g"] == 0
        assert body["milestone_text"] is None
        assert body["alcohol_recovery_hint"] is False
        assert body["caffeine_warning"] is None

    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(eating_disorder=True, daily_water_ml=2000),
    )
    def test_persistence_still_records_macros_internally(
        self, _ctx, proxy_user, seed, headers,
    ):
        """The wire response is stripped; the DB still keeps the truth so a
        future profile change re-exposes the data."""
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.kcal > 0


# ---------------------------------------------------------------------------
# Soft delete + restore
# ---------------------------------------------------------------------------


class TestSoftDeleteAndRestore:
    def test_delete_soft_removes_from_total(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 500}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        del_resp = c.delete(f"{WATER_URL}{entry_id}/", **headers)
        assert del_resp.status_code == status.HTTP_200_OK
        body = del_resp.json()["data"]
        assert body["deleted"] is True
        assert body["today_total_water_ml"] == 0
        # Soft-delete: row still present, deleted_at set
        entry = WaterEntry.objects.get(id=entry_id)
        assert entry.deleted_at is not None

    def test_delete_cascades_food_log(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        entry = WaterEntry.objects.get(id=r1.json()["data"]["entry_id"])
        food_log_id = entry.food_log_id
        assert food_log_id is not None
        c.delete(f"{WATER_URL}{entry.id}/", **headers)
        assert not FoodLog.objects.filter(id=food_log_id).exists()
        entry.refresh_from_db()
        assert entry.food_log_id is None

    def test_restore_within_window(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        c.delete(f"{WATER_URL}{entry_id}/", **headers)
        restore = c.post(f"{WATER_URL}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_200_OK
        body = restore.json()["data"]
        assert body["restored"] is True
        assert body["today_total_water_ml"] == 250
        entry = WaterEntry.objects.get(id=entry_id)
        assert entry.deleted_at is None

    def test_restore_after_window_returns_410(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        c.delete(f"{WATER_URL}{entry_id}/", **headers)
        # Move deleted_at into the past beyond the 15-min window
        WaterEntry.objects.filter(id=entry_id).update(
            deleted_at=datetime.now(dt_tz.utc) - timedelta(minutes=20),
        )
        restore = c.post(f"{WATER_URL}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_410_GONE

    def test_delete_other_users_entry_returns_404(self, proxy_user, seed, headers):
        c = APIClient()
        # Create entry as user A
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        # Try to delete as user B (different external id)
        User.objects.create(username="bot:999", role="client", is_proxy=True)
        other_headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:999",
        }
        resp = c.delete(f"{WATER_URL}{entry_id}/", **other_headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# GET /today/
# ---------------------------------------------------------------------------


class TestTodayEndpoint:
    def test_empty_returns_zeros(self, proxy_user, seed, headers):
        c = APIClient()
        resp = c.get(TODAY_URL, **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["entries"] == []
        assert body["today_total_water_ml"] == 0
        assert body["today_norm_water_ml"] == 2000

    def test_aggregates_per_category_cups(self, proxy_user, seed, headers):
        c = APIClient()
        _post_water(c, {"ml": 200, "beverage_slug": "kofe_chernyi"}, headers)
        _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        _post_water(c, {"ml": 200, "beverage_slug": "chai_zelenyi"}, headers)
        _post_water(c, {"ml": 250}, headers)  # plain water, neither
        resp = c.get(TODAY_URL, **headers)
        body = resp.json()["data"]
        assert body["today_total_coffee_cups"] == 2
        assert body["today_total_tea_cups"] == 1
        assert body["today_caffeine_mg"] > 0

    def test_excludes_soft_deleted(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 500}, headers)
        _post_water(c, {"ml": 250}, headers)
        c.delete(f"{WATER_URL}{r1.json()['data']['entry_id']}/", **headers)
        resp = c.get(TODAY_URL, **headers)
        body = resp.json()["data"]
        assert len(body["entries"]) == 1
        assert body["today_total_water_ml"] == 250


# ---------------------------------------------------------------------------
# Purge task
# ---------------------------------------------------------------------------


class TestPurgeOlderThan90Days:
    def test_purges_old_soft_deleted(self, proxy_user, seed):
        # Insert an old soft-deleted row directly
        old = WaterEntry.objects.create(
            user=proxy_user, ts=datetime.now(dt_tz.utc) - timedelta(days=120),
            ml=250, water_ml=250.0,
            deleted_at=datetime.now(dt_tz.utc) - timedelta(days=100),
            deleted_reason=WaterEntry.DeletedReason.USER_UNDO,
        )
        # And a recent soft-deleted row
        recent = WaterEntry.objects.create(
            user=proxy_user, ts=datetime.now(dt_tz.utc),
            ml=250, water_ml=250.0,
            deleted_at=datetime.now(dt_tz.utc) - timedelta(days=5),
            deleted_reason=WaterEntry.DeletedReason.USER_UNDO,
        )
        purged = purge_deleted_water_entries(older_than_days=90)
        assert purged == 1
        assert not WaterEntry.objects.filter(id=old.id).exists()
        assert WaterEntry.objects.filter(id=recent.id).exists()
