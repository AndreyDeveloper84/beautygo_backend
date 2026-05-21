"""Transactional outbox infrastructure — envelope builder + emit helper.

See ADR-0009 §Mandatory event contract for the canonical envelope shape.
Issue #486 introduced this package after a Code Reviewer found that the
emit sites were writing bare ``data`` dicts into ``OutboxEvent.payload``
without the envelope (event_version, event_id, occurred_at, actor,
correlation_id) required for cross-service consumption.
"""
from .envelope import build_envelope, emit_outbox_event, safe_tenant_id

__all__ = ["build_envelope", "emit_outbox_event", "safe_tenant_id"]
