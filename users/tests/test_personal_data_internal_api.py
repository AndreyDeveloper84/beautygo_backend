"""Tests for C5 internal personal-data endpoints (PILOT_CONTRACTS v1.3.0).

Surface under test:

* ``GET  /api/v1/internal/users/{id}/personal-data/export/`` (C5.1) —
  synchronous JSON: profile subset + full personal-context catalogue.
* ``DELETE /api/v1/internal/users/{id}/personal-data/`` (C5.2/AMD-006) —
  idempotent personal-context wipe + AMD-010 AnalyticsEvent audit
  without personal values.

Auth pattern mirrors test_internal_users_api.py: Bearer
``AYLA_INTERNAL_API_TOKEN``, no JWT, no X-App-Type.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from analytics.models import AnalyticsEvent
from users.models import Profile, User, UserPersonalContext


EXPORT_URL = "/api/v1/internal/users/{user_id}/personal-data/export/"
DELETE_URL = "/api/v1/internal/users/{user_id}/personal-data/"

PROFILE_KEYS = {"phone", "email", "full_name", "bio", "city"}


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    return "test-bearer"


@pytest.fixture
def api(bearer_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer_token}")
    return client


@pytest.fixture
def anon():
    return APIClient()


@pytest.fixture
def user(db):
    u = User.objects.create_user(
        username="pd-user", password="pass",
        role="client", phone="+79992220001", email="pd@example.com",
    )
    Profile.objects.filter(user=u).update(
        full_name="Полина Данных", bio="био", city="Пенза",
    )
    return u


@pytest.fixture
def user_with_context(user):
    UserPersonalContext.objects.create(
        user=user,
        diet_type="vegan",
        preferred_districts=["Центр"],
        price_range_min=Decimal("500.00"),
        workplace_district="Заводской",
        data_sources={"diet_type": "explicit"},
    )
    return user


# ---------------------------------------------------------------------------
# Auth + 404 — both endpoints
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuth:
    def test_export_requires_bearer(self, anon, user):
        resp = anon.get(EXPORT_URL.format(user_id=user.pk))
        # Repo convention (test_internal_users_api.py): 401 or 403.
        assert resp.status_code in (401, 403)

    def test_delete_requires_bearer(self, anon, user):
        resp = anon.delete(DELETE_URL.format(user_id=user.pk))
        assert resp.status_code in (401, 403)

    def test_wrong_bearer_rejected(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer nope")
        assert client.get(EXPORT_URL.format(user_id=user.pk)).status_code in (401, 403)
        assert client.delete(DELETE_URL.format(user_id=user.pk)).status_code in (401, 403)

    @pytest.mark.parametrize("url", [EXPORT_URL, DELETE_URL])
    def test_unknown_user_404(self, api, url):
        resp = api.generic("GET" if "export" in url else "DELETE",
                           url.format(user_id=uuid4()))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.parametrize("url", [EXPORT_URL, DELETE_URL])
    def test_soft_deleted_user_404(self, api, user, url):
        from django.utils import timezone

        user.deleted_at = timezone.now()
        user.save(update_fields=["deleted_at"])
        resp = api.generic("GET" if "export" in url else "DELETE",
                           url.format(user_id=user.pk))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# C5.1 Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_happy_path_full_payload(self, api, user_with_context):
        resp = api.get(EXPORT_URL.format(user_id=user_with_context.pk))
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert data["user_id"] == str(user_with_context.pk)
        assert data["exported_at"]  # ISO 8601 timestamp present

        # Profile subset — closed allowlist (extend deliberately only).
        assert set(data["profile"].keys()) == PROFILE_KEYS
        assert data["profile"]["phone"] == "+79992220001"
        assert data["profile"]["email"] == "pd@example.com"
        assert data["profile"]["full_name"] == "Полина Данных"
        assert data["profile"]["city"] == "Пенза"

        # Full personal-context catalogue — declared prefs round-trip.
        ctx = data["personal_context"]
        assert ctx["diet_type"] == "vegan"
        assert ctx["preferred_districts"] == ["Центр"]
        assert ctx["workplace_district"] == "Заводской"
        assert ctx["price_range_min"] == "500.00"  # §1: decimal as string
        assert ctx["data_sources"] == {"diet_type": "explicit"}

    def test_no_context_row_exports_null(self, api, user):
        resp = api.get(EXPORT_URL.format(user_id=user.pk))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["personal_context"] is None
        # Export must not CREATE data about the user (no lazy create).
        assert not UserPersonalContext.objects.filter(user=user).exists()

    def test_profile_missing_fields_are_empty_strings(self, api, db):
        bare = User.objects.create_user(
            username="pd-bare", password="pass", role="client",
        )
        resp = api.get(EXPORT_URL.format(user_id=bare.pk))
        assert resp.status_code == 200
        profile = resp.json()["data"]["profile"]
        assert set(profile.keys()) == PROFILE_KEYS
        assert profile["phone"] == ""
        assert profile["full_name"] == ""


# ---------------------------------------------------------------------------
# C5.2 Delete — idempotent + AMD-010 audit
# ---------------------------------------------------------------------------


class TestDelete:
    def test_deletes_context_and_reports_scope(self, api, user_with_context):
        """DRF-1366 — the cascade now leaves a tombstone instead of dropping
        the row, so tonight's inference cannot refill what it emptied. The
        wire contract is unchanged: scope reports what was actually removed.
        """
        resp = api.delete(DELETE_URL.format(user_id=user_with_context.pk))
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "user_id": str(user_with_context.pk),
            "deleted": ["personal_context"],
        }
        row = UserPersonalContext.objects.get(user=user_with_context)
        assert row.diet_type == ""
        assert row.preferred_districts == []
        assert set(row.data_sources.values()) == {"erased"}

    def test_idempotent_repeat_returns_200(self, api, user_with_context):
        url = DELETE_URL.format(user_id=user_with_context.pk)
        assert api.delete(url).status_code == 200
        second = api.delete(url)
        assert second.status_code == 200
        assert second.json()["data"]["deleted"] == []

    def test_delete_without_context_is_200(self, api, user):
        resp = api.delete(DELETE_URL.format(user_id=user.pk))
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] == []

    def test_audit_event_written_without_personal_values(
        self, api, user_with_context,
    ):
        api.delete(DELETE_URL.format(user_id=user_with_context.pk))
        event = AnalyticsEvent.objects.get(event_name="personal_data_deleted")
        assert event.actor_id == user_with_context.pk
        assert event.payload["user_id"] == str(user_with_context.pk)
        assert event.payload["scope"] == ["personal_context"]
        assert event.payload["initiator"] == "internal_api"
        # AMD-010: the deleted personal values must NOT be in the audit.
        payload_text = str(event.payload)
        assert "vegan" not in payload_text
        assert "Центр" not in payload_text
        assert "Заводской" not in payload_text

    def test_audit_written_on_repeat_too(self, api, user_with_context):
        url = DELETE_URL.format(user_id=user_with_context.pk)
        api.delete(url)
        api.delete(url)
        events = AnalyticsEvent.objects.filter(event_name="personal_data_deleted")
        assert events.count() == 2
        assert events.latest("created_at").payload["scope"] == []
