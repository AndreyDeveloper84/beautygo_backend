"""Tests for the cross-system webhook outbox (DRF-306).

Layered:
- Outbox helpers: enqueue creates a row, no-op when webhook URL empty,
  duplicate event_id is a no-op (idempotency).
- HMAC signature: deterministic, sha256= prefix.
- Delivery worker: 2xx → delivered, 5xx + network error → retry with
  backoff, after MAX_RETRIES → DLQ.
- Wiring: profile upsert / water create enqueue rows when URL set.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from nutrition.models import NutritionOutboxEvent
from nutrition.services.outbox_service import enqueue_event
from nutrition.webhook_delivery import (
    BACKOFF_SECONDS,
    MAX_RETRIES,
    compute_signature,
    deliver_pending,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-306"
WEBHOOK_URL = "https://maxbot.example/api/maxbot/ayla-events/"
WEBHOOK_SECRET = "shared-secret-bytes"


@pytest.fixture(autouse=True)
def _enable_webhook(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.NUTRITION_WEBHOOK_URL = WEBHOOK_URL
    settings.NUTRITION_WEBHOOK_SECRET = WEBHOOK_SECRET


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:306", role="client", is_proxy=True)


# ===========================================================================
# Outbox helpers
# ===========================================================================


class TestEnqueue:
    def test_creates_pending_row(self):
        row = enqueue_event(
            topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED,
            external_user_id="bot:1",
            payload={"x": 1},
        )
        assert row is not None
        assert row.status == NutritionOutboxEvent.Status.PENDING
        assert NutritionOutboxEvent.objects.count() == 1

    def test_no_op_when_url_empty(self, settings):
        settings.NUTRITION_WEBHOOK_URL = ""
        row = enqueue_event(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1",
            payload={"y": 2},
        )
        assert row is None
        assert NutritionOutboxEvent.objects.count() == 0

    def test_duplicate_event_id_is_idempotent(self):
        eid = str(uuid4())
        enqueue_event(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1",
            payload={"a": 1},
            event_id=eid,
        )
        enqueue_event(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
            external_user_id="bot:1",
            payload={"a": 2},  # different payload, same id
            event_id=eid,
        )
        assert NutritionOutboxEvent.objects.count() == 1
        # First write wins — payload not overwritten on duplicate.
        assert NutritionOutboxEvent.objects.get(id=eid).payload == {"a": 1}

    def test_unknown_topic_raises(self):
        with pytest.raises(ValueError):
            enqueue_event(
                topic="not_a_real_topic",
                external_user_id="bot:1",
                payload={},
            )


# ===========================================================================
# HMAC signature
# ===========================================================================


class TestHmac:
    def test_signature_is_deterministic_and_prefixed(self):
        sig = compute_signature("k", b"body")
        assert sig.startswith("sha256=")
        assert sig == compute_signature("k", b"body")

    def test_signature_changes_on_body_change(self):
        a = compute_signature("k", b"body1")
        b = compute_signature("k", b"body2")
        assert a != b


# ===========================================================================
# Delivery worker
# ===========================================================================


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return self._responder(url, content, headers)


@pytest.fixture
def make_event():
    def _make(topic=NutritionOutboxEvent.Topic.WATER_LOGGED):
        return NutritionOutboxEvent.objects.create(
            topic=topic,
            external_user_id="bot:1",
            payload={"k": "v"},
        )
    return _make


class TestDelivery:
    def test_2xx_marks_delivered(self, make_event):
        ev = make_event()
        client = _FakeClient(lambda *a: _FakeResponse(200, "ok"))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            counters = deliver_pending()
        assert counters["delivered"] == 1
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.DELIVERED
        assert ev.delivered_at is not None

    def test_signature_header_sent(self, make_event):
        make_event()
        client = _FakeClient(lambda *a: _FakeResponse(200))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_pending()
        sig = client.calls[0]["headers"]["X-Signature"]
        assert sig.startswith("sha256=")

    def test_5xx_schedules_retry_with_backoff(self, make_event):
        ev = make_event()
        client = _FakeClient(lambda *a: _FakeResponse(503, "down"))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_pending()
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.PENDING
        assert ev.retry_count == 1
        assert ev.next_retry_at is not None
        # First backoff slot
        assert "503" in ev.last_error

    def test_dlq_after_max_retries(self, make_event):
        ev = make_event()
        ev.retry_count = MAX_RETRIES - 1
        ev.save()
        client = _FakeClient(lambda *a: _FakeResponse(500))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_pending()
        ev.refresh_from_db()
        assert ev.status == NutritionOutboxEvent.Status.DLQ
        assert ev.retry_count == MAX_RETRIES

    def test_skips_rows_not_yet_eligible(self, make_event):
        ev = make_event()
        ev.next_retry_at = datetime.now(dt_tz.utc) + timedelta(seconds=60)
        ev.save()
        client = _FakeClient(lambda *a: _FakeResponse(200))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            counters = deliver_pending()
        assert counters["delivered"] == 0
        assert client.calls == []

    def test_no_op_when_url_empty(self, settings, make_event):
        make_event()
        settings.NUTRITION_WEBHOOK_URL = ""
        counters = deliver_pending()
        assert counters == {"delivered": 0, "retried": 0, "dlq": 0, "skipped": 0}

    def test_backoff_uses_known_schedule(self, make_event):
        ev = make_event()
        # Simulate first failure
        client = _FakeClient(lambda *a: _FakeResponse(500))
        with patch("nutrition.webhook_delivery.httpx.Client", return_value=client):
            deliver_pending()
        ev.refresh_from_db()
        # next_retry_at should be roughly now + BACKOFF_SECONDS[0]
        delta = (ev.next_retry_at - datetime.now(dt_tz.utc)).total_seconds()
        assert -1 < delta - BACKOFF_SECONDS[0] < 5  # allow scheduling slack


# ===========================================================================
# Wiring — profile + water emit when URL set
# ===========================================================================


class TestWiringProfileEmit:
    def test_profile_upsert_enqueues_event(self, proxy_user):
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:306",
        }
        c.post(
            "/api/v1/nutrition/internal/profile/",
            {"gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
             "goal": "maintain"},
            format="json",
            **headers,
        )
        rows = NutritionOutboxEvent.objects.filter(
            topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED,
            external_user_id="bot:306",
        )
        assert rows.count() == 1


class TestWiringWaterEmit:
    def test_water_post_enqueues_event(self, proxy_user):
        from django.core.management import call_command
        call_command("seed_beverages")
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:306",
        }
        c.post(
            "/api/v1/nutrition/internal/water/",
            {"ml": 250},
            format="json",
            **headers,
        )
        rows = NutritionOutboxEvent.objects.filter(
            topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
        )
        assert rows.count() == 1
