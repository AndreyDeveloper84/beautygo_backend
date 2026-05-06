"""Tests for POST /api/v1/analytics/event/.

Coverage:
  - Auth surface (anon JWT and real-user JWT both ingest)
  - Event-name validation against the code-side whitelist
  - Idempotency on (actor, client_event_id) — second POST returns 200
    with the same event id; per-anonymous-session variant tested too
  - app_type / tenant denormalisation comes from request middleware
  - 401 on no auth
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from analytics.event_catalogue import (
    AI_CHAT_MESSAGE_SENT,
    APP_OPENED,
    BOOKING_CREATED,
)
from analytics.models import AnalyticsEvent
from users.models import AnonymousSession, User


pytestmark = pytest.mark.django_db


URL = "/api/v1/analytics/event/"


@pytest.fixture
def real_user(db):
    return User.objects.create_user(
        username="real-1", password="x", role="client",
        phone="+79991110001",
    )


@pytest.fixture
def guest_user(db):
    user = User.objects.create_user(
        username="guest-1", password="x", role="client",
        phone="+79991110002", is_guest=True,
    )
    AnonymousSession.objects.create(
        device_id=uuid.uuid4(),
        user=user,
        platform=AnonymousSession.Platform.IOS,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    return user


@pytest.fixture
def auth_client(real_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=real_user)
    return c


@pytest.fixture
def guest_client(guest_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=guest_user)
    return c


def _payload(**overrides):
    body = {
        "event_name": APP_OPENED,
        "client_event_id": str(uuid.uuid4()),
        "client_timestamp": "2026-05-05T14:00:00Z",
        "payload": {"app_version": "1.2.3"},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(URL, _payload(), format="json")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_real_user_can_post(self, auth_client):
        resp = auth_client.post(URL, _payload(), format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        assert resp.json()["data"]["created"] is True

    def test_guest_user_can_post(self, guest_client):
        resp = guest_client.post(URL, _payload(), format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_event_name_returns_400_unknown_event(self, auth_client):
        resp = auth_client.post(
            URL,
            _payload(event_name="event_we_never_defined"),
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "UNKNOWN_EVENT_NAME"

    def test_missing_client_event_id_returns_400_validation(self, auth_client):
        body = _payload()
        del body["client_event_id"]
        resp = auth_client.post(URL, body, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_payload_must_be_object(self, auth_client):
        resp = auth_client.post(
            URL,
            _payload(payload="not-a-dict"),
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_payload_optional_defaults_to_empty(self, auth_client):
        body = _payload()
        del body["payload"]
        resp = auth_client.post(URL, body, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        ev = AnalyticsEvent.objects.get(id=resp.json()["data"]["id"])
        assert ev.payload == {}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_repeat_post_returns_200_same_id(self, auth_client, real_user):
        body = _payload()
        first = auth_client.post(URL, body, format="json")
        assert first.status_code == status.HTTP_201_CREATED
        first_id = first.json()["data"]["id"]

        second = auth_client.post(URL, body, format="json")
        assert second.status_code == status.HTTP_200_OK
        assert second.json()["data"]["id"] == first_id
        assert second.json()["data"]["created"] is False
        assert AnalyticsEvent.objects.filter(actor=real_user).count() == 1

    def test_different_client_event_ids_create_distinct_rows(
        self, auth_client, real_user,
    ):
        for _ in range(3):
            resp = auth_client.post(URL, _payload(), format="json")
            assert resp.status_code == status.HTTP_201_CREATED
        assert AnalyticsEvent.objects.filter(actor=real_user).count() == 3

    def test_guest_idempotency_per_anon_session(self, guest_client, guest_user):
        body = _payload()
        first = guest_client.post(URL, body, format="json")
        second = guest_client.post(URL, body, format="json")
        assert first.status_code == status.HTTP_201_CREATED
        assert second.status_code == status.HTTP_200_OK
        assert second.json()["data"]["id"] == first.json()["data"]["id"]
        # The row carries anonymous_session_id, not actor.
        ev = AnalyticsEvent.objects.get(id=first.json()["data"]["id"])
        assert ev.actor is None
        assert ev.anonymous_session_id == guest_user.anonymous_session.id


# ---------------------------------------------------------------------------
# Provenance fields
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_app_type_recorded_from_header(self, real_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=real_user)
        resp = c.post(URL, _payload(event_name=BOOKING_CREATED), format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        ev = AnalyticsEvent.objects.get(id=resp.json()["data"]["id"])
        assert ev.app_type == "pro"

    def test_event_name_and_payload_persisted(self, auth_client):
        body = _payload(
            event_name=AI_CHAT_MESSAGE_SENT,
            payload={"conversation_id": "abc-123", "tokens_in": 42},
        )
        resp = auth_client.post(URL, body, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        ev = AnalyticsEvent.objects.get(id=resp.json()["data"]["id"])
        assert ev.event_name == AI_CHAT_MESSAGE_SENT
        assert ev.payload["conversation_id"] == "abc-123"
        assert ev.payload["tokens_in"] == 42

    def test_client_timestamp_persisted_when_provided(self, auth_client):
        body = _payload(client_timestamp="2026-05-05T14:30:00Z")
        resp = auth_client.post(URL, body, format="json")
        ev = AnalyticsEvent.objects.get(id=resp.json()["data"]["id"])
        assert ev.client_timestamp is not None
        assert ev.client_timestamp.year == 2026


# ---------------------------------------------------------------------------
# Catalogue smoke
# ---------------------------------------------------------------------------


class TestCatalogue:
    def test_all_phase_0_phase_1_events_accepted(self, auth_client):
        # Walk a representative slice — booking + AI + nutrition + lifecycle.
        from analytics.event_catalogue import (
            BOOKING_VIEWED, BOOKING_CREATED,
            AI_CHAT_OPENED, AI_ACTION_CONFIRMED,
            FOOD_SCAN_TAKEN, WATER_LOGGED,
            APP_OPENED, ONBOARDING_COMPLETED,
        )
        for name in [
            BOOKING_VIEWED, BOOKING_CREATED, AI_CHAT_OPENED,
            AI_ACTION_CONFIRMED, FOOD_SCAN_TAKEN, WATER_LOGGED,
            APP_OPENED, ONBOARDING_COMPLETED,
        ]:
            resp = auth_client.post(URL, _payload(event_name=name), format="json")
            assert resp.status_code == status.HTTP_201_CREATED, (name, resp.json())
