"""Tests for water tracker endpoints + WaterService.

Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION:

POST /nutrition/water
    Request:  { amount_ml: 150 | 200 | 250 | 350 | 500 }
    Response: { water_ml, water_goal_ml, water_pct, log_id }

DELETE /nutrition/water/{id}
    Response: WaterLogResponse (with updated aggregate)

GET /nutrition/water/today
    Response: { logs: [{id, amount_ml, logged_at}], water_ml, water_goal_ml }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import WaterLog


pytestmark = pytest.mark.django_db


CREATE_URL = "/api/v1/nutrition/water/"
TODAY_URL = "/api/v1/nutrition/water/today/"


@pytest.fixture
def client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="wat-client", password="x", role="client",
        phone="+79995550000",
    )
    Profile.objects.filter(user=u).update(full_name="Wat", city="Penza")
    return u


@pytest.fixture
def other_client_user(db):
    from users.models import User

    u = User.objects.create_user(
        username="wat-other", password="x", role="client",
        phone="+79995550001",
    )
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_water(*, user, amount, when=None) -> WaterLog:
    return WaterLog.objects.create(
        user=user,
        amount_ml=amount,
        logged_at=when or datetime.now(dt_tz.utc),
    )


# ---------------------------------------------------------------------------
# Auth + app-type — apply to all three endpoints
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_post_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(CREATE_URL, {"amount_ml": 250}, format="json")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_post_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(CREATE_URL, {"amount_ml": 250}, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_today_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(TODAY_URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


class TestCreateValidation:
    @pytest.mark.parametrize("bad_amount", [0, 100, 199, 1000, -50])
    def test_invalid_amount_rejected(self, auth_client, bad_amount):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": bad_amount}, format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_amount_rejected(self, auth_client):
        resp = auth_client.post(CREATE_URL, {}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.parametrize("amount", [150, 200, 250, 350, 500])
    def test_all_spec_amounts_accepted(self, auth_client, amount):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": amount}, format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


class TestCreateHappyPath:
    def test_creates_log_and_returns_aggregate(self, auth_client, client_user):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert set(body.keys()) == {
            "water_ml", "water_goal_ml", "water_pct", "log_id",
        }
        assert body["water_ml"] == 250
        assert body["water_goal_ml"] == 2000
        # 250 / 2000 = 12.5 → rounded 12 or 13 (banker's rounding territory).
        assert body["water_pct"] in (12, 13)

        log = WaterLog.objects.get(id=body["log_id"])
        assert log.user_id == client_user.id
        assert log.amount_ml == 250

    def test_aggregate_sums_existing_today_logs(
        self, auth_client, client_user,
    ):
        _make_water(user=client_user, amount=250)
        _make_water(user=client_user, amount=350)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 200}, format="json",
        )
        body = resp.json()["data"]
        assert body["water_ml"] == 800

    def test_pct_caps_at_100(self, auth_client, client_user):
        # Already over goal — pct should clamp to 100.
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        body = resp.json()["data"]
        assert body["water_ml"] == 2250
        assert body["water_pct"] == 100

    def test_other_users_water_not_counted(
        self, auth_client, client_user, other_client_user,
    ):
        _make_water(user=other_client_user, amount=500)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        assert resp.json()["data"]["water_ml"] == 250


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDelete:
    def test_deletes_and_returns_updated_aggregate(
        self, auth_client, client_user,
    ):
        _make_water(user=client_user, amount=250)
        log = _make_water(user=client_user, amount=350)
        resp = auth_client.delete(f"/api/v1/nutrition/water/{log.id}/")
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert body["log_id"] == str(log.id)
        assert WaterLog.objects.filter(id=log.id).count() == 0

    def test_other_users_log_returns_404(
        self, auth_client, other_client_user,
    ):
        log = _make_water(user=other_client_user, amount=250)
        resp = auth_client.delete(f"/api/v1/nutrition/water/{log.id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        # Not actually deleted.
        assert WaterLog.objects.filter(id=log.id).count() == 1

    def test_unknown_id_returns_404(self, auth_client):
        from uuid import uuid4
        resp = auth_client.delete(f"/api/v1/nutrition/water/{uuid4()}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# GET /water/today
# ---------------------------------------------------------------------------


class TestToday:
    def test_empty_returns_empty_logs_zero_total(self, auth_client):
        resp = auth_client.get(TODAY_URL)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert set(body.keys()) == {"logs", "water_ml", "water_goal_ml"}
        assert body["logs"] == []
        assert body["water_ml"] == 0
        assert body["water_goal_ml"] == 2000

    def test_lists_today_logs_in_order(self, auth_client, client_user):
        now = datetime.now(dt_tz.utc)
        _make_water(user=client_user, amount=250, when=now - timedelta(hours=2))
        _make_water(user=client_user, amount=350, when=now - timedelta(hours=1))
        _make_water(user=client_user, amount=200, when=now)
        resp = auth_client.get(TODAY_URL)
        body = resp.json()["data"]
        assert body["water_ml"] == 800
        assert [log["amount_ml"] for log in body["logs"]] == [250, 350, 200]

    def test_excludes_yesterday(self, auth_client, client_user):
        yesterday = datetime.now(dt_tz.utc) - timedelta(days=1)
        # Force into yesterday's UTC day window.
        _make_water(
            user=client_user, amount=500,
            when=yesterday.replace(hour=12, minute=0, second=0),
        )
        _make_water(user=client_user, amount=250)
        resp = auth_client.get(TODAY_URL)
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert len(body["logs"]) == 1

    def test_excludes_other_users(
        self, auth_client, client_user, other_client_user,
    ):
        _make_water(user=other_client_user, amount=500)
        _make_water(user=client_user, amount=250)
        body = auth_client.get(TODAY_URL).json()["data"]
        assert body["water_ml"] == 250
        assert len(body["logs"]) == 1


# ---------------------------------------------------------------------------
# Integration with /nutrition/summary — Slice 3c stub now lives
# ---------------------------------------------------------------------------


class TestSummaryIntegration:
    def test_summary_water_now_reflects_water_logs(
        self, auth_client, client_user,
    ):
        # Make sure today's water is summed by the summary endpoint.
        _make_water(user=client_user, amount=250)
        _make_water(user=client_user, amount=350)
        today_iso = datetime.now(dt_tz.utc).date().isoformat()
        resp = auth_client.get(
            "/api/v1/nutrition/summary/", {"date": today_iso},
        )
        body = resp.json()["data"]
        assert body["water_ml"] == 600
        assert body["water_goal_ml"] == 2000
