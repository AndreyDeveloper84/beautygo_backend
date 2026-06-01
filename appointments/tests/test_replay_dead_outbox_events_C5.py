"""C5 — Tests for replay_dead_outbox_events management command.

The command resets dead-lettered ``OutboxEvent`` rows so the C2/C4
HTTP publisher re-attempts delivery. Failure mode if this command
is buggy: either (a) operator runs it after a transient outage and
the publisher never picks up the rows (silent data loss), or (b)
operator runs it with the wrong filter and re-triggers thousands of
duplicate webhook fan-outs.

Both modes are caught here:

* Selector gate: at least one of ``--tenant`` / ``--since`` /
  ``--event-id`` / ``--all`` must be set. No selector → CommandError.
* ``--all`` cannot combine with named filters — explicit opt-in
  for widening scope.
* ``--dry-run`` preserves all state. Re-run without it actually
  mutates. The DLQ predicate is always applied — non-DLQ rows are
  never touched even by ``--all``.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from appointments.models import OutboxEvent


def _dead_event(**kwargs) -> OutboxEvent:
    defaults = dict(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
        payload={"event_id": "x", "tenant_id": "t-1"},
        external_delivery_enabled=True,
        bot_delivery_status=OutboxEvent.BotDeliveryStatus.DEAD,
        bot_dead_lettered_at=timezone.now(),
        bot_attempt_count=8,
        bot_last_error="HTTP 500: server exploded",
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


def _pending_event(**kwargs) -> OutboxEvent:
    defaults = dict(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
        payload={"event_id": "y"},
        external_delivery_enabled=True,
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


@pytest.mark.django_db
class TestSelectorGate:
    """At least one selector is required — refuses to operate without scope."""

    def test_no_selector_raises(self):
        with pytest.raises(CommandError, match="No selector"):
            call_command("replay_dead_outbox_events", stdout=StringIO())

    def test_all_flag_unblocks_no_selector(self):
        _dead_event()
        out = StringIO()
        call_command(
            "replay_dead_outbox_events", "--all", "--dry-run", stdout=out,
        )
        # Dry-run printed the matched-count line but did not mutate.
        assert "Matched dead-lettered rows: 1" in out.getvalue()

    def test_all_combined_with_named_filter_raises(self):
        with pytest.raises(CommandError, match="--all cannot be combined"):
            call_command(
                "replay_dead_outbox_events",
                "--all", "--tenant", "t-1",
                stdout=StringIO(),
            )


@pytest.mark.django_db
class TestDryRun:
    """Dry-run prints matches but never mutates."""

    def test_dry_run_does_not_reset_rows(self):
        ev = _dead_event()
        call_command(
            "replay_dead_outbox_events", "--all", "--dry-run",
            stdout=StringIO(),
        )
        ev.refresh_from_db()
        assert ev.bot_delivery_status == OutboxEvent.BotDeliveryStatus.DEAD
        assert ev.bot_dead_lettered_at is not None
        assert ev.bot_attempt_count == 8


@pytest.mark.django_db
class TestReplay:
    """Without --dry-run, the matched rows are reset to pending."""

    def test_reset_clears_dlq_state(self):
        ev = _dead_event()
        call_command(
            "replay_dead_outbox_events", "--all", stdout=StringIO(),
        )
        ev.refresh_from_db()
        assert ev.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING
        assert ev.bot_dead_lettered_at is None
        assert ev.bot_attempt_count == 0
        assert ev.bot_next_retry_at is None
        # bot_last_error preserved so the post-replay audit trail
        # still names the original reason for the DLQ.
        assert "server exploded" in ev.bot_last_error

    def test_reset_only_touches_dlq_rows(self):
        # A non-DLQ row must never be reset by this command, even
        # under --all. The DLQ predicate is the safety floor.
        dead = _dead_event()
        pending = _pending_event(bot_delivery_status="pending")
        sent = _pending_event(
            bot_delivery_status="sent",
            bot_delivered_at=timezone.now(),
        )

        call_command(
            "replay_dead_outbox_events", "--all", stdout=StringIO(),
        )

        dead.refresh_from_db()
        pending.refresh_from_db()
        sent.refresh_from_db()
        assert dead.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING
        # Pending was already pending — untouched (the command would
        # be idempotent here anyway).
        assert pending.bot_delivery_status == "pending"
        # Sent rows must NEVER be reverted to pending — that would
        # double-deliver a successfully processed event.
        assert sent.bot_delivery_status == "sent"
        assert sent.bot_delivered_at is not None


@pytest.mark.django_db
class TestTenantFilter:
    """--tenant scopes via payload.tenant_id JSONB lookup."""

    def test_tenant_filter_matches_only_payload_tenant(self):
        target = _dead_event(payload={"event_id": "a", "tenant_id": "t-1"})
        other = _dead_event(payload={"event_id": "b", "tenant_id": "t-2"})

        call_command(
            "replay_dead_outbox_events", "--tenant", "t-1",
            stdout=StringIO(),
        )
        target.refresh_from_db()
        other.refresh_from_db()
        assert target.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING
        assert other.bot_delivery_status == OutboxEvent.BotDeliveryStatus.DEAD


@pytest.mark.django_db
class TestSinceFilter:
    """--since scopes by bot_dead_lettered_at."""

    def test_since_filter_includes_at_or_after(self):
        old = _dead_event(bot_dead_lettered_at=timezone.now() - timedelta(hours=4))
        new = _dead_event(bot_dead_lettered_at=timezone.now() - timedelta(minutes=10))

        cutoff = (timezone.now() - timedelta(minutes=30)).isoformat()
        call_command(
            "replay_dead_outbox_events", "--since", cutoff,
            stdout=StringIO(),
        )
        old.refresh_from_db()
        new.refresh_from_db()
        assert old.bot_delivery_status == OutboxEvent.BotDeliveryStatus.DEAD
        assert new.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING

    def test_malformed_since_raises(self):
        with pytest.raises(CommandError, match="ISO-8601"):
            call_command(
                "replay_dead_outbox_events", "--since", "yesterday",
                stdout=StringIO(),
            )


@pytest.mark.django_db
class TestEventIdFilter:
    """--event-id targets a single row for surgical replay."""

    def test_event_id_targets_single_row(self):
        target = _dead_event()
        other = _dead_event()

        call_command(
            "replay_dead_outbox_events", "--event-id", str(target.id),
            stdout=StringIO(),
        )
        target.refresh_from_db()
        other.refresh_from_db()
        assert target.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING
        assert other.bot_delivery_status == OutboxEvent.BotDeliveryStatus.DEAD
