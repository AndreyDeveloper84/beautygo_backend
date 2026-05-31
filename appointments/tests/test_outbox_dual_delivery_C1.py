"""C1 — OutboxEvent dual-delivery fields (Block C foundation).

Pins the schema additions from migration 0010 so a future model edit
that drops one of the new fields fails loudly before bot-platform
HTTP publisher (Block C) is wired against the production rows.

Two acceptance criteria the publisher will rely on, made explicit here:

1. Existing rows (created before this migration) stay backward
   compatible: ``external_delivery_enabled=False`` keeps the row
   invisible to the publisher. The legacy dispatcher path still works
   because it filters on ``processed_at IS NULL`` and not on any
   new field.

2. The new index ``outbox_publisher_scan_idx`` exists on the table.
   Without it the publisher scan would degrade to a full-table seek
   once the outbox grows past the cold-data threshold.
"""
from __future__ import annotations

import pytest
from django.db import connection

from appointments.models import OutboxEvent


@pytest.mark.django_db
class TestOutboxDualDeliveryFields:
    """Schema-pin tests for migration 0010."""

    def test_new_row_defaults_are_publisher_safe(self):
        ev = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"event_id": "11111111-1111-1111-1111-111111111111"},
        )

        assert ev.external_delivery_enabled is False, (
            "New rows must default to external_delivery_enabled=False so "
            "Block C's publisher cannot accidentally ship every legacy "
            "topic the moment the migration lands."
        )
        assert ev.external_target == "bot-platform"
        assert ev.bot_delivery_status == OutboxEvent.BotDeliveryStatus.PENDING
        assert ev.bot_attempt_count == 0
        assert ev.bot_delivered_at is None
        assert ev.bot_next_retry_at is None
        assert ev.bot_last_error == ""
        assert ev.bot_response_status is None
        assert ev.bot_dead_lettered_at is None
        assert ev.local_processed_at is None

    def test_existing_processed_at_unchanged_by_new_fields(self):
        # Simulate a legacy row touched by the current dispatcher: it
        # sets ``processed_at`` and leaves the new fields at defaults.
        # The publisher must NOT pick this row up because
        # ``external_delivery_enabled=False`` still gates delivery.
        from django.utils import timezone
        ev = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"event_id": "22222222-2222-2222-2222-222222222222"},
            processed_at=timezone.now(),
        )
        ev.refresh_from_db()

        assert ev.processed_at is not None  # legacy semantics preserved
        assert ev.external_delivery_enabled is False
        assert ev.local_processed_at is None  # publisher field untouched

    def test_publisher_scan_index_exists(self):
        # The composite index drives the publisher's main query —
        # losing it silently would tank scan cost. Pin the index name.
        index_names = {idx.name for idx in OutboxEvent._meta.indexes}
        assert "outbox_publisher_scan_idx" in index_names

        # And confirm the index is actually present in the DB schema
        # (catches a future migration that removes the Meta.indexes
        # entry but forgets to add a replacement). pg_indexes is
        # PostgreSQL-specific; on SQLite laptop runs the Meta-level
        # assertion above is the durable check.
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_indexes "
                "WHERE tablename = %s AND indexname = %s",
                [OutboxEvent._meta.db_table, "outbox_publisher_scan_idx"],
            )
            assert cur.fetchone() is not None, (
                "outbox_publisher_scan_idx missing from DB — migration "
                "0010 may have been skipped or rolled back."
            )

    def test_status_choices_cover_publisher_lifecycle(self):
        # Publisher state machine: pending → sent → acknowledged
        # (happy path), pending → failed → … → dead (retry exhaust).
        # Each state must be representable.
        choices = {choice for choice, _ in OutboxEvent.BotDeliveryStatus.choices}
        assert choices == {"pending", "sent", "acknowledged", "failed", "dead"}
