"""C2 + C4 — Cross-service HTTP publisher unit tests.

Closes codex P0-4 (outbox events created but not delivered). The
publisher is the second half of the dual-delivery contract: rows
with ``external_delivery_enabled=True`` get POSTed to bot-platform's
``/api/v1/internal/events/ingest`` while the existing local
dispatcher continues to handle in-process side effects.

Test surface:

* Happy path — 2xx → row marked ``sent`` with ``bot_delivered_at`` /
  ``bot_response_status`` populated, retry fields untouched.
* 5xx transient → ``failed`` with backoff schedule and incremented
  attempt counter.
* 4xx permanent (non-429) → immediate ``dead`` with
  ``bot_dead_lettered_at``, no further retry.
* 429 → transient (same path as 5xx).
* Network/timeout → ``failed`` with backoff.
* Retry budget exhaustion → ``dead`` after :data:`MAX_DELIVERY_ATTEMPTS`.
* Eligibility filter — rows with ``external_delivery_enabled=False``
  or future ``bot_next_retry_at`` are skipped.

Settings the tests need: ``BOT_PLATFORM_BASE_URL`` non-empty and
``AYLA_INTERNAL_API_TOKEN`` non-empty (publisher refuses to ship
otherwise — guarded by RuntimeError). We patch
``appointments.infrastructure.outbox.publisher._attempt_post``
directly to avoid taking a real HTTP dependency on the test.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from appointments.infrastructure.outbox import publisher
from appointments.infrastructure.outbox.publisher import (
    MAX_DELIVERY_ATTEMPTS,
    _backoff_seconds,
    publish_outbox_events_to_bot,
)
from appointments.models import OutboxEvent


_BASE_SETTINGS = override_settings(
    BOT_PLATFORM_BASE_URL="https://bot.test.local",
    BOT_PLATFORM_INGEST_PATH="/api/v1/internal/events/ingest",
    AYLA_INTERNAL_API_TOKEN="test-bearer",
)


def _make_event(**kwargs) -> OutboxEvent:
    defaults = dict(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
        payload={"event_id": "test", "event_name": "booking.created", "data": {}},
        external_delivery_enabled=True,
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


@pytest.mark.django_db
@_BASE_SETTINGS
class TestHappyPath:
    """2xx response → row marked sent with delivered_at populated."""

    def test_2xx_marks_row_sent(self):
        ev = _make_event()
        with patch.object(publisher, "_attempt_post", return_value=(200, "")):
            summary = publish_outbox_events_to_bot()

        ev.refresh_from_db()
        assert summary.sent == 1
        assert summary.failed == 0
        assert summary.dead == 0
        assert ev.bot_delivery_status == "sent"
        assert ev.bot_delivered_at is not None
        assert ev.bot_response_status == 200
        assert ev.bot_attempt_count == 1
        # Retry fields stay unset on a successful first delivery.
        assert ev.bot_next_retry_at is None
        assert ev.bot_dead_lettered_at is None

    def test_201_also_counted_as_sent(self):
        # ayla-bot ingest may return 201 for newly created dedupe row;
        # any 2xx must be honoured.
        ev = _make_event()
        with patch.object(publisher, "_attempt_post", return_value=(201, "")):
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "sent"


@pytest.mark.django_db
@_BASE_SETTINGS
class TestTransientFailure:
    """5xx / network / 429 → ``failed`` + backoff schedule."""

    def test_500_schedules_retry(self):
        ev = _make_event()
        with patch.object(publisher, "_attempt_post", return_value=(500, "boom")):
            summary = publish_outbox_events_to_bot()

        ev.refresh_from_db()
        assert summary.failed == 1
        assert ev.bot_delivery_status == "failed"
        assert ev.bot_attempt_count == 1
        assert ev.bot_response_status == 500
        assert "boom" in ev.bot_last_error
        assert ev.bot_next_retry_at is not None
        # 30s base, attempt 1 → +60s. Allow ±5s for timezone.now() drift
        # between publisher and assertion.
        delta = (ev.bot_next_retry_at - timezone.now()).total_seconds()
        assert 55 <= delta <= 65, f"expected ~60s, got {delta}s"

    def test_429_is_treated_as_transient(self):
        # Per the C2 spec, 429 (rate-limited) follows the retry path
        # rather than dead-lettering immediately.
        ev = _make_event()
        with patch.object(publisher, "_attempt_post", return_value=(429, "slow down")):
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "failed"
        assert ev.bot_next_retry_at is not None

    def test_network_error_uses_backoff(self):
        # ``_attempt_post`` returns (None, "Timeout: ...") on
        # network-layer failures. Treated the same as 5xx.
        ev = _make_event()
        with patch.object(
            publisher, "_attempt_post", return_value=(None, "Timeout: 10s"),
        ):
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "failed"
        assert ev.bot_response_status is None
        assert "Timeout" in ev.bot_last_error
        assert ev.bot_next_retry_at is not None


@pytest.mark.django_db
@_BASE_SETTINGS
class TestPermanentFailure:
    """4xx (except 429) → immediate dead-letter."""

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    def test_4xx_immediately_dead_letters(self, status_code):
        ev = _make_event()
        with patch.object(
            publisher, "_attempt_post", return_value=(status_code, "client bug"),
        ):
            summary = publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert summary.dead == 1
        assert ev.bot_delivery_status == "dead"
        assert ev.bot_dead_lettered_at is not None
        assert ev.bot_response_status == status_code
        # Retry not scheduled — operator must use C5 replay command.
        assert ev.bot_next_retry_at is None


@pytest.mark.django_db
@_BASE_SETTINGS
class TestRetryBudget:
    """Exhausting :data:`MAX_DELIVERY_ATTEMPTS` transitions to dead."""

    def test_attempt_at_limit_dead_letters(self):
        # Simulate the row being on attempt 7 already; one more 500
        # makes attempt 8, which is the limit → dead.
        ev = _make_event(
            bot_delivery_status="failed",
            bot_attempt_count=MAX_DELIVERY_ATTEMPTS - 1,
        )
        with patch.object(publisher, "_attempt_post", return_value=(500, "still bad")):
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "dead"
        assert ev.bot_attempt_count == MAX_DELIVERY_ATTEMPTS
        assert ev.bot_dead_lettered_at is not None


class TestBackoffCurve:
    """C4 — exponential backoff with 1h cap."""

    @pytest.mark.parametrize(
        "attempt,expected",
        [
            (1, 30),         # base
            (2, 60),
            (3, 120),
            (4, 240),
            (5, 480),
            (6, 960),
            (7, 1920),
            (8, 3600),       # capped at 1h
            (9, 3600),       # still capped
            (100, 3600),     # absurd input — still capped, not overflowed
        ],
    )
    def test_curve_doubles_then_caps_at_one_hour(self, attempt, expected):
        assert _backoff_seconds(attempt) == expected


@pytest.mark.django_db
@_BASE_SETTINGS
class TestEligibility:
    """Only rows with the right gate combination are picked up."""

    def test_external_delivery_disabled_rows_are_skipped(self):
        ev = _make_event(external_delivery_enabled=False)
        with patch.object(publisher, "_attempt_post") as mock_post:
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "pending"  # untouched default
        mock_post.assert_not_called()

    def test_future_retry_time_skips_row(self):
        ev = _make_event(
            bot_delivery_status="failed",
            bot_next_retry_at=timezone.now() + timedelta(minutes=10),
        )
        with patch.object(publisher, "_attempt_post") as mock_post:
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "failed"  # still pending retry
        mock_post.assert_not_called()

    def test_past_retry_time_is_eligible(self):
        ev = _make_event(
            bot_delivery_status="failed",
            bot_attempt_count=2,
            bot_next_retry_at=timezone.now() - timedelta(seconds=1),
        )
        with patch.object(publisher, "_attempt_post", return_value=(200, "")):
            publish_outbox_events_to_bot()
        ev.refresh_from_db()
        assert ev.bot_delivery_status == "sent"

    def test_terminal_states_are_not_re_attempted(self):
        # 'sent' / 'dead' / 'acknowledged' rows should never be picked
        # up — they're either done or awaiting operator intervention.
        for status in ("sent", "dead", "acknowledged"):
            ev = _make_event(bot_delivery_status=status)
            with patch.object(publisher, "_attempt_post") as mock_post:
                publish_outbox_events_to_bot()
            ev.refresh_from_db()
            assert ev.bot_delivery_status == status
            mock_post.assert_not_called()


@pytest.mark.django_db
@_BASE_SETTINGS
class TestHeaders:
    """Idempotency contract — event_id reused on retries."""

    def test_idempotency_key_header_carries_event_id(self):
        ev = _make_event()
        captured = {}

        def fake_post(url, json, headers, timeout):  # noqa: A002 — match requests sig
            captured["headers"] = headers
            captured["url"] = url

            class _Resp:
                status_code = 200
                text = "ok"
            return _Resp()

        with patch.object(publisher.requests, "post", side_effect=fake_post):
            publish_outbox_events_to_bot()

        assert captured["url"].endswith("/api/v1/internal/events/ingest")
        assert captured["headers"]["X-Idempotency-Key"] == str(ev.id)
        assert captured["headers"]["Authorization"] == "Bearer test-bearer"
