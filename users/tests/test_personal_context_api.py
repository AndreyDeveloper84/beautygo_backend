"""Integration tests for /api/v1/users/me/personal-context/ — DRF-174."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ai.tests.factories import make_user
from users.models import UserPersonalContext


pytestmark = pytest.mark.django_db


URL = "/api/v1/users/me/personal-context/"


@pytest.fixture
def client_user(db):
    return make_user(role="client")


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuth:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        assert c.get(URL).status_code == 401

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        assert c.get(URL).status_code == 403

    def test_specialist_role_returns_403(self):
        spec = make_user(role="specialist")
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=spec)
        assert c.get(URL).status_code == 403

    def test_anonymous_guest_returns_403(self):
        """Regression — surfaced 2026-04-27 dev-VPS smoke test:
        anonymous users (is_guest=True, role='client') was reaching this
        endpoint because IsClient only checked role. Now IsClient also
        rejects is_guest=True, matching the Gate model in spec v2.0."""
        guest = make_user(role="client", is_guest=True)
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=guest)
        assert c.get(URL).status_code == 403


# ---------------------------------------------------------------------------
# GET — defaults + auto-creation behaviour
# ---------------------------------------------------------------------------


class TestRead:
    def test_get_returns_defaults_for_new_user(self, auth_client, client_user):
        # First-ever GET — no row exists, should return defaults.
        resp = auth_client.get(URL)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["preferred_districts"] == []
        assert data["preferred_time_slots"] == []
        assert data["price_range_min"] is None
        assert data["price_range_max"] is None
        assert data["diet_type"] == ""
        assert data["skin_sensitivities"] == []
        assert data["prefers_flexible_cancellation"] is False
        # GET should NOT create a row — verify.
        assert not UserPersonalContext.objects.filter(user=client_user).exists()

    def test_get_returns_persisted_values(self, auth_client, client_user):
        UserPersonalContext.objects.create(
            user=client_user,
            preferred_districts=["Замоскворечье", "Хамовники"],
            preferred_time_slots=["evening", "morning"],
            price_range_max=Decimal("3000"),
            diet_type="vegan",
        )
        resp = auth_client.get(URL)
        data = resp.json()["data"]
        assert data["preferred_districts"] == ["Замоскворечье", "Хамовники"]
        assert "evening" in data["preferred_time_slots"]
        assert data["price_range_max"] == "3000.00"
        assert data["diet_type"] == "vegan"


# ---------------------------------------------------------------------------
# PATCH — partial updates + validation
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_patch_creates_row_on_first_write(self, auth_client, client_user):
        resp = auth_client.patch(
            URL, {"preferred_districts": ["Тверская"]}, format="json",
        )
        assert resp.status_code == 200
        assert UserPersonalContext.objects.filter(user=client_user).exists()

    def test_patch_updates_only_provided_fields(self, auth_client, client_user):
        UserPersonalContext.objects.create(
            user=client_user, preferred_districts=["Old"], diet_type="vegan",
        )
        resp = auth_client.patch(
            URL, {"preferred_districts": ["New"]}, format="json",
        )
        data = resp.json()["data"]
        assert data["preferred_districts"] == ["New"]
        # diet_type untouched
        assert data["diet_type"] == "vegan"

    def test_patch_rejects_invalid_time_slot(self, auth_client):
        resp = auth_client.patch(
            URL, {"preferred_time_slots": ["midnight"]}, format="json",
        )
        assert resp.status_code == 400
        assert "preferred_time_slots" in resp.json()["error"]["details"]

    def test_patch_dedupes_time_slots(self, auth_client):
        resp = auth_client.patch(
            URL, {"preferred_time_slots": ["morning", "morning", "evening"]},
            format="json",
        )
        assert resp.json()["data"]["preferred_time_slots"] == [
            "morning", "evening",
        ]

    def test_patch_validates_diet_choice(self, auth_client):
        resp = auth_client.patch(URL, {"diet_type": "raw_meat"}, format="json")
        assert resp.status_code == 400

    def test_patch_caps_districts_at_20(self, auth_client):
        big = [f"District-{i}" for i in range(30)]
        resp = auth_client.patch(
            URL, {"preferred_districts": big}, format="json",
        )
        assert resp.status_code == 200
        # Capped server-side.
        assert len(resp.json()["data"]["preferred_districts"]) == 20

    def test_patch_rejects_negative_price(self, auth_client):
        resp = auth_client.patch(
            URL, {"price_range_min": "-100"}, format="json",
        )
        assert resp.status_code == 400

    def test_patch_rejects_max_below_min(self, auth_client):
        resp = auth_client.patch(
            URL,
            {"price_range_min": "2000", "price_range_max": "1000"},
            format="json",
        )
        assert resp.status_code == 400

    def test_patch_accepts_valid_price_range(self, auth_client):
        resp = auth_client.patch(
            URL,
            {"price_range_min": "500", "price_range_max": "5000"},
            format="json",
        )
        assert resp.status_code == 200

    def test_patch_rejects_long_district_name(self, auth_client):
        too_long = "x" * 200
        resp = auth_client.patch(
            URL, {"preferred_districts": [too_long]}, format="json",
        )
        assert resp.status_code == 400

    def test_patch_rejects_non_list_districts(self, auth_client):
        resp = auth_client.patch(
            URL, {"preferred_districts": "Тверская"}, format="json",
        )
        assert resp.status_code == 400

    def test_patch_can_set_flexible_cancellation_flag(
        self, auth_client, client_user,
    ):
        resp = auth_client.patch(
            URL, {"prefers_flexible_cancellation": True}, format="json",
        )
        assert resp.json()["data"]["prefers_flexible_cancellation"] is True
