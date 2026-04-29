"""Tests for notifications list/read endpoints — Slice N3.

Per Notion API Spec v2.0 §NOTIFICATIONS:

  GET    /api/v1/notifications/?is_read=&page=&page_size=
  PATCH  /api/v1/notifications/{id}/read/
  POST   /api/v1/notifications/read-all/

Coverage: auth + app-type guards, query validation, pagination,
is_read filter, ownership 404s, mark-read idempotency, mark-all-read
counts, and the unread_count aggregate that mobile uses for badge.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from notifications.models import Notification
from users.models import User


pytestmark = pytest.mark.django_db


LIST_URL = "/api/v1/notifications/"
READ_ALL_URL = "/api/v1/notifications/read-all/"


def _read_url(notification_id) -> str:
    return f"/api/v1/notifications/{notification_id}/read/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="nv-client", password="x", role="client",
        phone="+79998880000",
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="nv-other", password="x", role="client",
        phone="+79998880001",
    )


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_notification(*, user, template_id="appointment_created_client",
                       title="t", body="b", is_read=False) -> Notification:
    return Notification.objects.create(
        user=user,
        template_id=template_id,
        channel=Notification.Channel.PUSH,
        title=title, body=body, data={"x": 1},
        deep_link="ayla-client://x", status=Notification.Status.SENT,
        is_read=is_read,
    )


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_list_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(LIST_URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.get(LIST_URL)
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_read_all_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(READ_ALL_URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /notifications/
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_returns_empty_results_zero_counts(self, auth_client):
        resp = auth_client.get(LIST_URL)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body == {"results": [], "count": 0, "unread_count": 0}

    def test_returns_only_owners_notifications(
        self, auth_client, client_user, other_user,
    ):
        _make_notification(user=client_user, title="mine")
        _make_notification(user=other_user, title="not mine")
        body = auth_client.get(LIST_URL).json()["data"]
        assert body["count"] == 1
        assert len(body["results"]) == 1
        assert body["results"][0]["title"] == "mine"

    def test_response_shape_has_spec_fields(self, auth_client, client_user):
        n = _make_notification(user=client_user)
        body = auth_client.get(LIST_URL).json()["data"]
        item = body["results"][0]
        assert set(item.keys()) == {
            "id", "user_id", "type", "title",
            "body", "data", "is_read", "created_at",
        }
        # template_id → type mapping
        assert item["type"] == "appointment_created_client"
        assert item["id"] == str(n.id)
        assert item["user_id"] == str(client_user.id)

    def test_unread_count_independent_of_filter(
        self, auth_client, client_user,
    ):
        _make_notification(user=client_user, is_read=False)
        _make_notification(user=client_user, is_read=False)
        _make_notification(user=client_user, is_read=True)

        # Filter for read-only — count should reflect filter, but
        # unread_count is full-set.
        body = auth_client.get(LIST_URL, {"is_read": "true"}).json()["data"]
        assert body["count"] == 1
        assert body["unread_count"] == 2

    def test_is_read_filter_false(self, auth_client, client_user):
        _make_notification(user=client_user, is_read=False, title="unread")
        _make_notification(user=client_user, is_read=True, title="read")
        body = auth_client.get(LIST_URL, {"is_read": "false"}).json()["data"]
        titles = [n["title"] for n in body["results"]]
        assert titles == ["unread"]

    def test_pagination_defaults_to_page_size_20(
        self, auth_client, client_user,
    ):
        for i in range(25):
            _make_notification(user=client_user, title=f"n{i}")
        body = auth_client.get(LIST_URL).json()["data"]
        assert body["count"] == 25
        assert len(body["results"]) == 20

    def test_invalid_is_read_returns_400(self, auth_client):
        resp = auth_client.get(LIST_URL, {"is_read": "tru"})
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# PATCH /notifications/{id}/read/
# ---------------------------------------------------------------------------


class TestRead:
    def test_marks_unread_as_read(self, auth_client, client_user):
        n = _make_notification(user=client_user, is_read=False)
        resp = auth_client.patch(_read_url(n.id))
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["data"]["is_read"] is True
        n.refresh_from_db()
        assert n.is_read is True

    def test_already_read_is_idempotent(self, auth_client, client_user):
        n = _make_notification(user=client_user, is_read=True)
        resp = auth_client.patch(_read_url(n.id))
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"]["is_read"] is True

    def test_other_users_notification_returns_404(
        self, auth_client, other_user,
    ):
        foreign = _make_notification(user=other_user)
        resp = auth_client.patch(_read_url(foreign.id))
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        # Ensure the foreign notification was NOT modified.
        foreign.refresh_from_db()
        assert foreign.is_read is False

    def test_unknown_id_returns_404(self, auth_client):
        resp = auth_client.patch(_read_url(uuid.uuid4()))
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# POST /notifications/read-all/
# ---------------------------------------------------------------------------


class TestReadAll:
    def test_marks_all_unread_returns_count(self, auth_client, client_user):
        _make_notification(user=client_user, is_read=False)
        _make_notification(user=client_user, is_read=False)
        _make_notification(user=client_user, is_read=True)
        resp = auth_client.post(READ_ALL_URL)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"] == {"marked_count": 2}
        # All rows now read.
        unread = Notification.objects.filter(
            user=client_user, is_read=False,
        ).count()
        assert unread == 0

    def test_does_not_touch_other_users(
        self, auth_client, client_user, other_user,
    ):
        _make_notification(user=other_user, is_read=False)
        _make_notification(user=client_user, is_read=False)
        resp = auth_client.post(READ_ALL_URL)
        assert resp.json()["data"]["marked_count"] == 1
        # Other user's row stays unread.
        assert Notification.objects.filter(
            user=other_user, is_read=False,
        ).count() == 1

    def test_no_unread_returns_zero(self, auth_client, client_user):
        _make_notification(user=client_user, is_read=True)
        resp = auth_client.post(READ_ALL_URL)
        assert resp.json()["data"] == {"marked_count": 0}

    def test_empty_returns_zero(self, auth_client):
        resp = auth_client.post(READ_ALL_URL)
        assert resp.json()["data"] == {"marked_count": 0}
