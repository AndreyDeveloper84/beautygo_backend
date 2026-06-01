"""C3 — HMAC + timestamp anti-replay headers on the outbox publisher.

Joint-aware with Gamma's bot-side verifier in
ai-bot-platform-codex apps/eventbus/ingest_security.py. The contract:

* Header ``X-Ayla-Event-Signature: sha256=<hex>`` with HMAC-SHA256
  over the raw request body using the shared secret.
* Header ``X-Ayla-Event-Timestamp: <unix_ms>`` — bot rejects on
  drift > 300s.
* Same body bytes used for signing and for the wire; we
  pre-serialise to bytes once and pass ``data=body_bytes`` to
  ``requests.post`` so JSON encoding cannot drift between sign-time
  and send-time.

These tests recompute the expected signature against the body bytes
the publisher actually shipped, then assert the header value matches.
The bot-side verifier runs the same compare with
``hmac.compare_digest``; if these signatures match here, they verify
on the bot too.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from appointments.infrastructure.outbox import publisher
from appointments.infrastructure.outbox.publisher import (
    _sign_body,
    publish_outbox_events_to_bot,
)
from appointments.models import OutboxEvent


@pytest.fixture
def hmac_settings(settings):
    """Apply the publisher's HMAC-enabled settings for a single test.

    Used by the per-test fixture below. Pulled out so the "no secret"
    test can override the secret to '' without dragging in the
    autouse contract.
    """
    settings.BOT_PLATFORM_BASE_URL = "https://bot.test.local"
    settings.BOT_PLATFORM_INGEST_PATH = "/api/v1/internal/events/ingest"
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    settings.AYLA_OUTBOUND_HMAC_SECRET = "shared-secret-32-bytes-min-long-enough"
    return settings


def _make_event(**kwargs) -> OutboxEvent:
    defaults = dict(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
        payload={
            "event_id": "01J9HXKM8Z2T4V6R8Q1P3D5F7E",
            "event_name": "booking.created",
            "event_version": 1,
            "data": {"appointment_id": "abc"},
        },
        external_delivery_enabled=True,
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


class TestSignBodyHelper:
    """Pure helper — independent of HTTP layer."""

    def test_signature_format_is_sha256_hex(self):
        sig = _sign_body(b'{"a":1}', ts_ms=0, secret="s")
        assert sig.startswith("sha256=")
        hex_part = sig[len("sha256="):]
        assert len(hex_part) == 64
        int(hex_part, 16)  # round-trip parse confirms valid hex

    def test_signature_is_deterministic(self):
        sig1 = _sign_body(b'{"a":1}', ts_ms=0, secret="s")
        sig2 = _sign_body(b'{"a":1}', ts_ms=0, secret="s")
        assert sig1 == sig2

    def test_different_body_yields_different_signature(self):
        sig1 = _sign_body(b'{"a":1}', ts_ms=0, secret="s")
        sig2 = _sign_body(b'{"a":2}', ts_ms=0, secret="s")
        assert sig1 != sig2

    def test_different_secret_yields_different_signature(self):
        sig1 = _sign_body(b'{"a":1}', ts_ms=0, secret="s1")
        sig2 = _sign_body(b'{"a":1}', ts_ms=0, secret="s2")
        assert sig1 != sig2

    def test_signature_matches_bot_side_verify_recipe(self):
        # Recompute the way ai-bot-platform-codex apps/eventbus/
        # ingest_security.py verifies. If this assertion passes here,
        # bot-side verify_signature() returns ok=True for the same
        # (body, secret) pair.
        body = b'{"event":"booking.created"}'
        secret = "shared-secret-xyz"
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256,
        ).hexdigest()
        assert _sign_body(body, ts_ms=42, secret=secret) == expected


@pytest.mark.django_db
class TestPublisherAddsHmacHeaders:
    """End-to-end: publisher attaches signature + timestamp on every POST."""

    @pytest.fixture(autouse=True)
    def _apply_hmac_settings(self, hmac_settings):
        # Reuse the module-level fixture so the same secret value lives
        # in one place and the per-class scope picks it up.
        return hmac_settings

    def _capture_post(self):
        """Return (capture_dict, side_effect) for patching requests.post."""
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None, **_kwargs):
            captured["url"] = url
            captured["body"] = data
            captured["headers"] = headers
            captured["timeout"] = timeout

            class _Resp:
                status_code = 200
                text = "ok"
            return _Resp()

        return captured, fake_post

    def test_signature_header_present(self):
        _make_event()
        captured, fake_post = self._capture_post()
        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        sig = captured["headers"]["X-Ayla-Event-Signature"]
        assert sig.startswith("sha256=")

    def test_timestamp_header_present_and_unix_ms(self):
        _make_event()
        captured, fake_post = self._capture_post()
        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        ts = captured["headers"]["X-Ayla-Event-Timestamp"]
        ts_ms = int(ts)
        # Sanity: timestamp must be a unix-ms value in the current era.
        # 2024-01-01 = 1704067200000 ms; an order of magnitude check
        # catches a unit mistake (seconds vs ms).
        assert 1_700_000_000_000 <= ts_ms <= 9_999_999_999_999

    def test_signature_verifies_against_request_body(self):
        # Recompute on the exact bytes the publisher shipped — proving
        # there's no drift between sign-time and send-time. Bot-side
        # verify_signature() uses hmac.compare_digest with the same
        # inputs, so a match here is a match there.
        _make_event()
        captured, fake_post = self._capture_post()
        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        body_bytes = captured["body"]
        sig_header = captured["headers"]["X-Ayla-Event-Signature"]
        expected = "sha256=" + hmac.new(
            b"shared-secret-32-bytes-min-long-enough",
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        assert sig_header == expected

    def test_body_bytes_are_json_serialisation_of_envelope(self):
        ev = _make_event()
        captured, fake_post = self._capture_post()
        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        # Body must parse as JSON and equal the envelope. The publisher
        # sorts keys + uses compact separators so two runs produce the
        # same bytes — important for the HMAC to be reproducible.
        parsed = json.loads(captured["body"])
        assert parsed == ev.payload


@pytest.mark.django_db
class TestNoSecretFailsClosed:
    """Empty secret → no headers → bot rejects every request.

    The fail-closed behaviour is the point of C3. A forgotten secret
    in prod must NOT silently bypass HMAC; the bot side handles the
    miss (`REASON_MISSING_SIGNATURE`). On the publisher side we just
    confirm no header is attached so the bot's "missing" path triggers.
    """

    def test_empty_secret_skips_signature_headers(self, settings):
        # Same as the no-secret path on prod: bot rejects with
        # REASON_MISSING_SIGNATURE. Use the per-test settings fixture
        # (not the autouse hmac one) so we can flip the secret off
        # without dragging in the default.
        settings.BOT_PLATFORM_BASE_URL = "https://bot.test.local"
        settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
        settings.AYLA_OUTBOUND_HMAC_SECRET = ""
        _make_event()
        captured = {}

        def fake_post(url, data=None, headers=None, timeout=None, **_kwargs):
            captured["headers"] = headers

            class _Resp:
                status_code = 200
                text = "ok"
            return _Resp()

        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        # Neither header attached — body is unsigned. Bot rejects.
        assert "X-Ayla-Event-Signature" not in captured["headers"]
        assert "X-Ayla-Event-Timestamp" not in captured["headers"]
