"""Contract test for the ADR-0009 outbox envelope shape (#486).

Cross-service consumers (bot-platform handlers per ADR-0009 §Domain
ownership matrix) dedupe by ``event_id`` and version-route by
``(event_name, event_version)``. Both guarantees collapse silently if
the envelope shape drifts. This test pins the keys + types so any
producer-side change to the wrapper structure fails CI loudly before
the next consumer roll-out.

The test does NOT pin domain-data keys — those belong to each
emit-site's own integration test. Only the envelope wrapper is the
cross-service contract.
"""
from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

import pytest

from appointments.infrastructure.outbox.envelope import (
    EVENT_VERSIONS,
    build_envelope,
    emit_outbox_event,
)
from appointments.models import OutboxEvent


REQUIRED_KEYS = {
    "event_id",
    "event_name",
    "event_version",
    "occurred_at",
    "tenant_id",
    "user_id",
    "actor",
    "correlation_id",
    "causation_id",
    "data",
}

# ISO-8601 with optional offset / Z. Django's timezone.now().isoformat()
# emits e.g. '2026-05-21T18:42:11.928765+00:00'.
ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


class TestBuildEnvelopeShape:
    """build_envelope() — pure function, no DB."""

    def test_has_all_required_top_level_keys(self):
        env = build_envelope(event_name="booking.created", data={})
        assert set(env.keys()) == REQUIRED_KEYS

    def test_event_id_is_uuid(self):
        env = build_envelope(event_name="booking.created", data={})
        # Raises ValueError if not a valid UUID — that's the assertion.
        UUID(env["event_id"])

    def test_event_id_is_unique_across_calls(self):
        env_a = build_envelope(event_name="booking.created", data={})
        env_b = build_envelope(event_name="booking.created", data={})
        assert env_a["event_id"] != env_b["event_id"]

    def test_event_name_matches_topic(self):
        env = build_envelope(event_name="booking.cancelled", data={})
        assert env["event_name"] == "booking.cancelled"

    def test_event_version_pinned_from_registry(self):
        # Bumping the version in EVENT_VERSIONS is a deliberate contract
        # break; this assertion catches an accidental bump.
        env = build_envelope(event_name="booking.created", data={})
        assert env["event_version"] == EVENT_VERSIONS["booking.created"]
        assert isinstance(env["event_version"], int)

    def test_unknown_event_name_raises(self):
        with pytest.raises(ValueError, match="Unregistered event_name"):
            build_envelope(event_name="bogus.thing", data={})

    def test_occurred_at_is_iso8601(self):
        env = build_envelope(event_name="booking.created", data={})
        assert ISO_RE.match(env["occurred_at"]), env["occurred_at"]
        # Round-trips through fromisoformat.
        parsed = datetime.fromisoformat(env["occurred_at"])
        assert parsed.tzinfo is not None, "occurred_at must be tz-aware"

    def test_actor_defaults_to_system(self):
        env = build_envelope(event_name="booking.created", data={})
        assert env["actor"] == "system"

    def test_actor_accepts_user_admin_system(self):
        for actor in ("system", "user", "admin"):
            env = build_envelope(
                event_name="booking.created", data={}, actor=actor,
            )
            assert env["actor"] == actor

    def test_correlation_id_generated_when_omitted(self):
        env = build_envelope(event_name="booking.created", data={})
        UUID(env["correlation_id"])

    def test_correlation_id_preserved_when_passed(self):
        cid = "11111111-2222-3333-4444-555555555555"
        env = build_envelope(
            event_name="booking.created", data={}, correlation_id=cid,
        )
        assert env["correlation_id"] == cid

    def test_causation_id_none_by_default(self):
        env = build_envelope(event_name="booking.created", data={})
        assert env["causation_id"] is None

    def test_tenant_and_user_id_stringified(self):
        import uuid as _uuid
        tid = _uuid.uuid4()
        uid = _uuid.uuid4()
        env = build_envelope(
            event_name="booking.created",
            data={},
            tenant_id=tid,
            user_id=uid,
        )
        assert env["tenant_id"] == str(tid)
        assert env["user_id"] == str(uid)

    def test_data_passed_through_verbatim(self):
        payload = {"booking_id": "abc", "amount": "1500.00"}
        env = build_envelope(event_name="booking.created", data=payload)
        assert env["data"] == payload
        # Identity NOT required (callers may freeze the dict later);
        # equality is the cross-service contract.


@pytest.mark.django_db
class TestEmitOutboxEventIntegration:
    """emit_outbox_event() — touches DB; asserts row + envelope agree."""

    def test_creates_row_with_envelope_payload(self):
        event = emit_outbox_event(
            topic="booking.created",
            data={"booking_id": "abc-123"},
        )
        # Row in DB; envelope is in payload.
        assert OutboxEvent.objects.filter(id=event.id).exists()
        assert event.payload["event_name"] == "booking.created"
        assert event.payload["event_version"] == 1
        assert event.payload["data"] == {"booking_id": "abc-123"}

    def test_row_pk_matches_envelope_event_id(self):
        """The whole point of generating the UUID once: row.id and
        payload.event_id agree, so dedupe by either yields the same
        decision."""
        event = emit_outbox_event(
            topic="booking.created",
            data={"booking_id": "abc-123"},
        )
        assert str(event.id) == event.payload["event_id"]

    def test_data_property_returns_inner_dict(self):
        event = emit_outbox_event(
            topic="booking.created",
            data={"booking_id": "abc-123", "amount": "1500.00"},
        )
        # Convenience accessor — handlers read via .data, not .payload.
        assert event.data == {"booking_id": "abc-123", "amount": "1500.00"}

    def test_data_property_legacy_fallback(self):
        """Pre-#486 rows have no envelope wrapper — .data falls back
        to .payload for backward compat. Verified by manually
        constructing such a row (won't happen in new code)."""
        row = OutboxEvent.objects.create(
            topic="booking.created",
            payload={"booking_id": "legacy-abc"},
        )
        # No 'data' key in payload → fallback returns payload unchanged.
        assert row.data == {"booking_id": "legacy-abc"}


class TestEventVersionsRegistry:
    """Every Topic must have a version registered so the dispatcher
    can fail fast on an unregistered emit (the dispatcher itself
    routes by topic; consumers route by (event_name, event_version))."""

    def test_all_outbox_topics_have_a_version(self):
        missing = []
        for value, _label in OutboxEvent.Topic.choices:
            if value not in EVENT_VERSIONS:
                missing.append(value)
        assert not missing, (
            f"Topics without an entry in EVENT_VERSIONS: {missing}. "
            "Add them to appointments/infrastructure/outbox/envelope.py."
        )
