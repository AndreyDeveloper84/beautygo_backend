"""Tests for GET /api/v1/nutrition/summary/.

Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION:

Query: date? (YYYY-MM-DD, default today)
Response 200 (NutritionSummaryResponse):
    date, calories_total, calories_goal, protein_g, fat_g, carbs_g,
    water_ml, water_goal_ml, entries[], vitamin_deficits

Slice 3c stubs:
- water_ml = 0, water_goal_ml = settings default 2000 (Slice 4 fills)
- vitamin_deficits = {} (Slice 3a' fills)
"""
from __future__ import annotations

from datetime import date, datetime, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog


pytestmark = pytest.mark.django_db


URL = "/api/v1/nutrition/summary/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="sum-client", password="x", role="client",
        phone="+79994440000",
    )
    Profile.objects.filter(user=u).update(full_name="Sum User", city="Penza")
    return u


@pytest.fixture
def other_client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="sum-other", password="x", role="client",
        phone="+79994440001",
    )
    Profile.objects.filter(user=u).update(full_name="Other", city="Penza")
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_log(*, user, dish, calories, protein, fat, carbs, when, meal="lunch"):
    return FoodLog.objects.create(
        user=user, dish_name=dish, portion_multiplier=1.0,
        calories=calories, protein_g=protein, fat_g=fat, carbs_g=carbs,
        meal_type=meal, logged_at=when,
    )


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.get(URL)
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Query validation
# ---------------------------------------------------------------------------


class TestQueryValidation:
    def test_invalid_date_format_returns_400(self, auth_client):
        resp = auth_client.get(URL, {"date": "29-04-2026"})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_garbage_date_returns_400(self, auth_client):
        resp = auth_client.get(URL, {"date": "tomorrow"})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Empty day — zeros + stubs
# ---------------------------------------------------------------------------


class TestEmptyDay:
    def test_no_entries_returns_zero_totals_and_default_goals(self, auth_client):
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        # Spec NutritionSummaryResponse keys
        assert set(body.keys()) == {
            "date", "calories_total", "calories_goal",
            "protein_g", "fat_g", "carbs_g",
            "water_ml", "water_goal_ml",
            "entries", "vitamin_deficits",
        }
        assert body["date"] == "2026-04-29"
        assert body["calories_total"] == 0
        assert body["protein_g"] == 0
        assert body["fat_g"] == 0
        assert body["carbs_g"] == 0
        assert body["entries"] == []
        # Slice 3c stubs
        assert body["water_ml"] == 0
        assert body["water_goal_ml"] == 2000
        assert body["vitamin_deficits"] == {}
        # Settings default
        assert body["calories_goal"] == 2000

    def test_default_date_is_today(self, auth_client):
        resp = auth_client.get(URL)
        assert resp.status_code == status.HTTP_200_OK
        today = datetime.now(dt_tz.utc).date().isoformat()
        assert resp.json()["data"]["date"] == today


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_sums_macros_for_day(self, auth_client, client_user):
        when = datetime(2026, 4, 29, 12, 0, tzinfo=dt_tz.utc)
        _make_log(user=client_user, dish="борщ",
                  calories=147, protein=4.8, fat=6.6, carbs=20.1,
                  when=when, meal="lunch")
        _make_log(user=client_user, dish="оливье",
                  calories=200, protein=5.5, fat=16.5, carbs=7.8,
                  when=when.replace(hour=14), meal="lunch")
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        body = resp.json()["data"]
        assert body["calories_total"] == 347.0
        assert body["protein_g"] == pytest.approx(10.3, rel=1e-2)
        assert body["fat_g"] == pytest.approx(23.1, rel=1e-2)
        assert body["carbs_g"] == pytest.approx(27.9, rel=1e-2)
        assert len(body["entries"]) == 2

    def test_excludes_entries_from_other_days(self, auth_client, client_user):
        # Yesterday + today, request today only.
        yest = datetime(2026, 4, 28, 23, 0, tzinfo=dt_tz.utc)
        today = datetime(2026, 4, 29, 12, 0, tzinfo=dt_tz.utc)
        _make_log(user=client_user, dish="вчера",
                  calories=500, protein=20, fat=20, carbs=50,
                  when=yest)
        _make_log(user=client_user, dish="сегодня",
                  calories=147, protein=4.8, fat=6.6, carbs=20.1,
                  when=today)
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        body = resp.json()["data"]
        assert body["calories_total"] == 147.0
        assert len(body["entries"]) == 1
        assert body["entries"][0]["dish_name"] == "сегодня"

    def test_excludes_entries_from_other_users(
        self, auth_client, client_user, other_client_user,
    ):
        when = datetime(2026, 4, 29, 12, 0, tzinfo=dt_tz.utc)
        _make_log(user=other_client_user, dish="чужой",
                  calories=999, protein=99, fat=99, carbs=99, when=when)
        _make_log(user=client_user, dish="мой",
                  calories=147, protein=4.8, fat=6.6, carbs=20.1, when=when)
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        body = resp.json()["data"]
        assert body["calories_total"] == 147.0
        assert len(body["entries"]) == 1
        assert body["entries"][0]["dish_name"] == "мой"

    def test_entries_ordered_by_logged_at(self, auth_client, client_user):
        d = date(2026, 4, 29)
        late = datetime(d.year, d.month, d.day, 19, 0, tzinfo=dt_tz.utc)
        early = datetime(d.year, d.month, d.day, 8, 0, tzinfo=dt_tz.utc)
        _make_log(user=client_user, dish="ужин",
                  calories=300, protein=10, fat=10, carbs=30,
                  when=late, meal="dinner")
        _make_log(user=client_user, dish="завтрак",
                  calories=100, protein=5, fat=2, carbs=15,
                  when=early, meal="breakfast")
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        names = [e["dish_name"] for e in resp.json()["data"]["entries"]]
        assert names == ["завтрак", "ужин"]


# ---------------------------------------------------------------------------
# Day boundary — UTC interpretation
# ---------------------------------------------------------------------------


class TestDayBoundary:
    def test_includes_23_59_utc(self, auth_client, client_user):
        when = datetime(2026, 4, 29, 23, 59, 30, tzinfo=dt_tz.utc)
        _make_log(user=client_user, dish="x",
                  calories=10, protein=1, fat=1, carbs=1, when=when)
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        assert len(resp.json()["data"]["entries"]) == 1

    def test_excludes_next_day_00_00_utc(self, auth_client, client_user):
        when = datetime(2026, 4, 30, 0, 0, 30, tzinfo=dt_tz.utc)
        _make_log(user=client_user, dish="next-day",
                  calories=10, protein=1, fat=1, carbs=1, when=when)
        resp = auth_client.get(URL, {"date": "2026-04-29"})
        assert len(resp.json()["data"]["entries"]) == 0
