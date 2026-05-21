"""ADR-0009 §Mandatory event contract — envelope builder + outbox emit helper.

ADR-0009 line 104 specifies `event_id` as a ULID. We use UUID4 instead
so the value can double as `OutboxEvent.id` (a UUIDField PK) — gives us
row-PK ↔ envelope-event-id agreement without an extra column. UUIDs and
ULIDs are both opaque-for-dedupe to consumers; the deviation is documented
in the PR body for #486 and intentional.

The envelope shape (per ADR-0009):

    {
      "event_id":       "<UUID>",            // unique; consumers dedupe by this
      "event_name":     "booking.created",   // dot.notation; matches Topic
      "event_version":  1,                   // bump on breaking change
      "occurred_at":    "ISO8601 UTC",
      "tenant_id":      "<UUID> | null",
      "user_id":        "<UUID>",            // canonical Ayla User
      "actor":          "system | user | admin",
      "correlation_id": "<UUID>",            // trace related events
      "causation_id":   "<UUID> | null",     // what caused this event
      "data":           { ... }              // domain payload
    }

All cross-service consumers (bot-platform per ADR-0009 §Domain ownership
matrix) MUST dedupe by ``event_id`` and version-route handlers by
``(event_name, event_version)``. Without the envelope, those guarantees
collapse — pre-#486 emit sites produced "headless" payloads that any
duplicate delivery would silently re-process.

``OutboxEvent.id`` (UUID PK) is reused as the envelope's ``event_id`` so
the DB row and the payload agree without a second round-trip.
``OutboxEvent.topic`` mirrors ``event_name`` for SQL-side filtering.
"""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from django.utils import timezone


# ----------------------------------------------------------------------
# Versioning registry
# ----------------------------------------------------------------------
# Source of truth for the version of each event topic. Consumers register
# handlers per (event_name, event_version) — incrementing here forces
# them to add a new handler entry (and continue supporting the old
# version for ≥30 days per ADR-0009 §"Versioning rule").
#
# All MVP topics ship at v1. Bump only when the ``data`` schema changes
# in a backward-incompatible way (new required field, removed field,
# type change). Adding optional fields is non-breaking → no bump.

EVENT_VERSIONS: dict[str, int] = {
    "booking.created": 1,
    "booking.confirmed": 1,
    "booking.cancelled": 1,
    "booking.rescheduled": 1,
    "booking.completed": 1,
    "booking.no_show": 1,
    "payment.confirmed": 1,
    "payment.refunded": 1,
    "cache.invalidate_slots": 1,
}


VALID_ACTORS = frozenset({"system", "user", "admin"})


def _coerce_uuid(value: str | UUID | None) -> str | None:
    """Normalise UUID/str/None → str | None. Defensive against callers that
    pass already-stringified ids."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def build_envelope(
    *,
    event_name: str,
    data: dict[str, Any],
    actor: str = "system",
    user_id: str | UUID | None = None,
    tenant_id: str | UUID | None = None,
    correlation_id: str | UUID | None = None,
    causation_id: str | UUID | None = None,
    event_id: str | UUID | None = None,
    event_version: int | None = None,
) -> dict[str, Any]:
    """Wrap ``data`` in the ADR-0009 envelope.

    Defaults that fire when the caller omits a field:
    - ``event_id`` → fresh ``uuid.uuid4()`` (caller should pass the
      ``OutboxEvent.id`` it will use so the row PK and the payload agree).
    - ``event_version`` → looked up in ``EVENT_VERSIONS``; KeyError if
      the event_name is unknown — forces explicit registration on every
      new topic.
    - ``actor`` → ``"system"`` (most domain events are system-driven).
    - ``correlation_id`` → fresh UUID; pass an existing id to chain
      related events (e.g. a refund inheriting the booking's correlation).
    - ``causation_id`` → ``None`` (only set when the event was triggered
      by another event).
    - ``tenant_id`` / ``user_id`` → ``None`` (caller MUST pass them for
      tenant-scoped + user-attributed events; consumers may reject
      envelopes missing required ids).

    Returns a dict suitable to assign to ``OutboxEvent.payload`` verbatim.
    """
    if actor not in VALID_ACTORS:
        raise ValueError(
            f"actor must be one of {sorted(VALID_ACTORS)}, got {actor!r}. "
            "ADR-0009 §Mandatory event contract pins these three values; "
            "extend VALID_ACTORS deliberately if a fourth ever lands."
        )
    if event_version is None:
        try:
            event_version = EVENT_VERSIONS[event_name]
        except KeyError as exc:
            raise ValueError(
                f"Unregistered event_name {event_name!r}; add it to "
                "EVENT_VERSIONS in appointments/infrastructure/outbox/"
                "envelope.py before emitting."
            ) from exc

    return {
        "event_id": _coerce_uuid(event_id) or str(uuid.uuid4()),
        "event_name": event_name,
        "event_version": event_version,
        "occurred_at": timezone.now().isoformat(),
        "tenant_id": _coerce_uuid(tenant_id),
        "user_id": _coerce_uuid(user_id),
        "actor": actor,
        "correlation_id": _coerce_uuid(correlation_id) or str(uuid.uuid4()),
        "causation_id": _coerce_uuid(causation_id),
        "data": data,
    }


def emit_outbox_event(
    *,
    topic: str,
    data: dict[str, Any],
    actor: str = "system",
    user_id: str | UUID | None = None,
    tenant_id: str | UUID | None = None,
    correlation_id: str | UUID | None = None,
    causation_id: str | UUID | None = None,
):
    """Create an ``OutboxEvent`` row with the full ADR-0009 envelope.

    Generates the row's UUID PK first and reuses it as the envelope
    ``event_id`` so consumers can dedupe by either the DB row id or
    the payload field — they're the same value.

    Caller must be inside an ``@transaction.atomic`` block (the dispatcher
    expects rows to be committed atomically with the domain change that
    emitted them; otherwise the dispatcher can pick up a row that
    references state which never committed).

    Returns the created ``OutboxEvent`` instance so the caller can read
    ``.id`` if needed (e.g. for logging or for chaining a follow-up event
    via ``causation_id``).
    """
    # Local import — OutboxEvent lives in appointments.models which
    # imports this module's parent package (transitively via the
    # appointments app). Module-level import would deadlock app loading.
    from appointments.models import OutboxEvent

    # Crisp failure at the emit site for a topic typo (e.g.
    # 'bookings.created' s/'booking.created'). Without this guard, the
    # ValueError would be raised inside build_envelope deeper in the
    # call stack, after the @transaction.atomic block has already done
    # work that's about to be rolled back.
    valid_topics = {value for value, _label in OutboxEvent.Topic.choices}
    if topic not in valid_topics:
        raise ValueError(
            f"Unknown OutboxEvent.Topic: {topic!r}. "
            f"Add to OutboxEvent.Topic in appointments/models.py + "
            f"EVENT_VERSIONS in this module."
        )

    event_id = uuid.uuid4()
    envelope = build_envelope(
        event_name=topic,
        data=data,
        actor=actor,
        user_id=user_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        event_id=event_id,
    )
    return OutboxEvent.objects.create(
        id=event_id,
        topic=topic,
        payload=envelope,
    )
