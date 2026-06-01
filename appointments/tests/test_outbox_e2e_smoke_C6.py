"""C6 — End-to-end smoke for the outbox → bot-platform delivery chain.

Per handoff Block C → C6. Verifies the full chain Ayla-side:

  1. Domain code emits an ``OutboxEvent`` via ``emit_outbox_event``.
  2. The row is gated for cross-service delivery
     (``external_delivery_enabled=True``).
  3. The publisher beat task fires.
  4. Publisher constructs the ADR-0009 envelope, signs it (HMAC + ts),
     POSTs to bot-platform ``/api/v1/internal/events/ingest``.
  5. On 2xx, the row is marked ``sent`` with ``bot_delivered_at``.
  6. Bot-side HMAC verification recipe matches the publisher's signature
     (recomputed here against the exact body bytes shipped).

The bot-side ingest endpoint is mocked. The shared fixtures from
Gamma's A10 are NOT yet on dev; once they land a follow-up will
swap the inline envelope dict for a fixture path. The contract we
assert here mirrors the contract Gamma will then verify:

* envelope fields per docs/architecture/event-contract.md §2
* ``X-Idempotency-Key`` header carries ``OutboxEvent.id``
* ``X-Ayla-Event-Signature`` carries ``sha256=<hex>`` of the body
* ``X-Ayla-Event-Timestamp`` carries unix milliseconds

Per the handoff, this is the Ayla half of the joint PR. Gamma authors
the bot-side verification half (matching consumer + IngestDedupe row
assertion). When both halves land the same test exercises both sides.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from appointments.infrastructure.outbox import publisher
from appointments.infrastructure.outbox.envelope import emit_outbox_event
from appointments.infrastructure.outbox.publisher import (
    publish_outbox_events_to_bot,
)
from appointments.models import OutboxEvent


SECRET = "shared-secret-32-bytes-min-long-enough"


@pytest.fixture
def publisher_settings(settings):
    """Wire the publisher's env so the chain actually fires.

    Without these the publisher raises RuntimeError before the first
    POST. We use a non-empty HMAC secret so the signed-headers branch
    runs — bot-side verify recipe is asserted below.
    """
    settings.BOT_PLATFORM_BASE_URL = "https://bot.test.local"
    settings.BOT_PLATFORM_INGEST_PATH = "/api/v1/internal/events/ingest"
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    settings.AYLA_OUTBOUND_HMAC_SECRET = SECRET
    return settings


@pytest.mark.django_db(transaction=True)
class TestOutboxToBotChain:
    """Full chain: emit → publisher → POST → row marked sent."""

    def _patch_bot_ingest(self, capture: dict):
        """Replace requests.post with a stub that captures the request
        and returns 200, simulating a successful bot-side ingest."""

        def fake_post(url, data=None, headers=None, timeout=None, **_kwargs):
            capture["url"] = url
            capture["body"] = data
            capture["headers"] = headers

            class _Resp:
                status_code = 200
                text = '{"status": "accepted"}'
            return _Resp()

        return patch.object(publisher.requests, "post", side_effect=fake_post)

    def test_chain_marks_row_sent_with_signed_envelope(
        self, publisher_settings,
    ):
        # 1. Domain code emits an outbox row. We use the real helper
        #    so the envelope (event_id, event_name, event_version,
        #    occurred_at, actor, correlation_id) gets built end-to-end
        #    — not a hand-crafted payload that could mask a contract
        #    drift in the envelope builder itself.
        event = emit_outbox_event(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            data={
                "booking_id": "11111111-1111-1111-1111-111111111111",
                "specialist_id": "22222222-2222-2222-2222-222222222222",
                "service_id": "33333333-3333-3333-3333-333333333333",
                "start_at": "2026-06-15T10:00:00+03:00",
                "end_at": "2026-06-15T11:00:00+03:00",
                "price_total": "1800.00",
                "source": "mobile_app",
            },
            actor="user",
            user_id="44444444-4444-4444-4444-444444444444",
            tenant_id="55555555-5555-5555-5555-555555555555",
        )

        # 2. Gate the row for cross-service delivery. Until Block C's
        #    per-topic opt-in lands in domain emit sites, the gate is
        #    flipped explicitly per row by the test.
        event.external_delivery_enabled = True
        event.save(update_fields=["external_delivery_enabled"])

        # 3. Publisher runs against the gated row with bot ingest
        #    stubbed.
        capture: dict = {}
        with self._patch_bot_ingest(capture):
            summary = publish_outbox_events_to_bot()

        assert summary.sent == 1
        assert summary.failed == 0
        assert summary.dead == 0

        # 4. Row state advances to 'sent' with delivery timestamp.
        event.refresh_from_db()
        assert event.bot_delivery_status == "sent"
        assert event.bot_delivered_at is not None
        assert event.bot_response_status == 200

        # 5. Wire-format pin — verifies envelope reached bot in the
        #    shape consumers expect. Per event-contract.md §2.1.
        body = json.loads(capture["body"])
        assert body["event_name"] == "booking.created"
        assert body["event_version"] == 1
        assert body["actor"] == "user"
        assert body["tenant_id"] == "55555555-5555-5555-5555-555555555555"
        assert body["user_id"] == "44444444-4444-4444-4444-444444444444"
        assert body["data"]["booking_id"] == "11111111-1111-1111-1111-111111111111"
        # event_id present, ULID/UUID shape (>= 22 chars after stripping
        # hyphens). Don't over-pin the exact format — only that it's
        # populated.
        assert body["event_id"]
        assert body["correlation_id"]

        # 6. Headers — auth, idempotency, HMAC signature, timestamp.
        headers = capture["headers"]
        assert headers["Authorization"] == "Bearer test-bearer"
        assert headers["X-Idempotency-Key"] == str(event.id)
        sig = headers["X-Ayla-Event-Signature"]
        ts = headers["X-Ayla-Event-Timestamp"]
        assert sig.startswith("sha256=")
        assert int(ts) > 1_700_000_000_000  # unix-ms in current era

        # 7. Bot-side verify recipe: recompute HMAC on the exact bytes
        #    that hit the wire. If this assertion passes here,
        #    ai-bot-platform-codex apps/eventbus/ingest_security.py
        #    verify_signature() returns ok=True for the same inputs.
        expected = "sha256=" + hmac.new(
            SECRET.encode("utf-8"),
            capture["body"].encode("utf-8") if isinstance(capture["body"], str)
            else capture["body"],
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected

    def test_chain_retries_on_5xx_and_succeeds_on_recovery(
        self, publisher_settings,
    ):
        # Realistic ops scenario: bot ingest blips with 502, the
        # publisher backs off, second tick recovers. The row must
        # land in 'sent' with attempt_count=2 — not stuck in 'failed'.
        event = emit_outbox_event(
            topic=OutboxEvent.Topic.BOOKING_CONFIRMED,
            data={"booking_id": "11111111-1111-1111-1111-111111111111"},
            actor="system",
            tenant_id="55555555-5555-5555-5555-555555555555",
        )
        event.external_delivery_enabled = True
        event.save(update_fields=["external_delivery_enabled"])

        # First tick: 502 → row marked 'failed' with backoff.
        with patch.object(
            publisher, "_attempt_post", return_value=(502, "bad gateway"),
        ):
            summary = publish_outbox_events_to_bot()
        assert summary.failed == 1
        event.refresh_from_db()
        assert event.bot_delivery_status == "failed"
        assert event.bot_attempt_count == 1

        # Clear the retry gate so the next tick picks the row up
        # immediately (skip the real backoff in test wall-clock).
        event.bot_next_retry_at = None
        event.save(update_fields=["bot_next_retry_at"])

        # Second tick: 200 → row marked 'sent', attempt counter
        # accumulates so ops can see the retry history.
        with patch.object(publisher, "_attempt_post", return_value=(200, "")):
            summary = publish_outbox_events_to_bot()
        assert summary.sent == 1
        event.refresh_from_db()
        assert event.bot_delivery_status == "sent"
        assert event.bot_attempt_count == 2
        assert event.bot_delivered_at is not None
