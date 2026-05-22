"""Idempotency helper for mutating booking endpoints (#512).

Usage in a view::

    from appointments.infrastructure.idempotency import (
        IdempotencyConflict, IdempotencyInFlight,
        lookup_or_open_idempotency, record_response,
    )

    def cancel(self, request, pk):
        try:
            cached, record = lookup_or_open_idempotency(
                request,
                operation_name="booking.cancel",
                target_type="Appointment",
                target_id=str(pk),
            )
        except IdempotencyConflict:
            return error_response(
                "IDEMPOTENCY_CONFLICT",
                "X-Idempotency-Key reused with a different body.",
                status_code=422,
            )
        except IdempotencyInFlight:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT",
                "Same key already being processed; retry in a moment.",
                status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = ... build success OR error response ...
        # MUST call on every return path (success AND error) so
        # error responses are also replay-safe. Stripe semantics.
        if record is not None:
            record_response(record, response.status_code, response.data)
        return response

Contract:
- ``lookup_or_open_idempotency`` returns ``(cached, record)``:
  - ``(None, None)`` — no X-Idempotency-Key header. Pass-through.
  - ``(cached, None)`` — replay hit. Cached has keys ``status`` and ``payload``.
  - ``(None, record)`` — fresh key. Caller MUST call ``record_response``
    on every return path so future replays get the same answer.
- Raises ``IdempotencyConflict`` — same key, different body. Client bug.
- Raises ``IdempotencyInFlight`` — placeholder row exists with
  ``response_status=0``, meaning a concurrent first call hasn't written
  its response yet OR a prior crash left a placeholder. Client should
  retry after backoff; ops can delete the row to force re-execution.

Body normalisation:
- ``json.dumps(body, sort_keys=True, separators=(',', ':'), default=str)``
  then SHA256. Stable across whitespace, key order, JSON-compatible
  types. ``default=str`` handles UUID / datetime / Decimal in the body.

target_id is audit-only:
- Lookup tuple is ``(user, operation_name, key)``. target_type +
  target_id are NOT in the unique constraint. A client that reuses
  the same key value across different target rows for the same
  operation receives a replay of the FIRST target's response —
  intentional: the idempotency contract is "same key = same answer."
  Clients must use distinct keys for distinct logical operations.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from appointments.models import IdempotencyKey


class IdempotencyConflict(Exception):
    """X-Idempotency-Key reused with a different request body."""


class IdempotencyInFlight(Exception):
    """Placeholder row exists but no response stored yet.

    Two scenarios produce this:
    - Concurrent first call: another request with the same key
      created the placeholder microseconds ago and is still executing.
      Client should retry after a brief backoff.
    - Prior crash: a request crashed before calling ``record_response``,
      leaving a placeholder. Next replay can't safely re-execute
      (operation may have committed side-effects pre-crash) and can't
      safely cache. 409 is the right "ambiguous, retry" signal; ops
      can delete the row to force re-execution.
    """


def _hash_body(body: Any) -> str:
    """SHA256 of the JSON-canonical form of ``body``."""
    if body is None:
        body = {}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_or_in_flight(
    existing: IdempotencyKey,
    body_hash: str,
    key: str,
) -> dict:
    """Resolve a found row to either a cached payload or raise.

    Shared by the initial-lookup and post-IntegrityError re-lookup
    paths — both need identical resolution semantics.
    """
    if existing.request_body_hash != body_hash:
        raise IdempotencyConflict(
            f"X-Idempotency-Key '{key}' reused with a different body."
        )
    if existing.response_status == 0:
        raise IdempotencyInFlight(
            f"X-Idempotency-Key '{key}' is still being processed."
        )
    return {
        "status": existing.response_status,
        "payload": existing.response_payload,
    }


def lookup_or_open_idempotency(
    request,
    *,
    operation_name: str,
    target_type: str = "",
    target_id: str = "",
) -> tuple[dict | None, IdempotencyKey | None]:
    """Look up the (user, operation, key) tuple from X-Idempotency-Key.

    Returns ``(cached, record)`` per the module docstring. May raise
    ``IdempotencyConflict`` or ``IdempotencyInFlight``.
    """
    key = (request.META.get("HTTP_X_IDEMPOTENCY_KEY") or "").strip()
    if not key:
        return None, None

    body_hash = _hash_body(getattr(request, "data", {}) or {})

    # Initial lookup — fast path for replays.
    existing = IdempotencyKey.objects.filter(
        user=request.user,
        operation_name=operation_name,
        key=key,
    ).first()

    if existing is not None:
        if existing.expires_at <= timezone.now():
            # TTL expired — delete and fall through to fresh create.
            existing.delete()
        else:
            return _existing_or_in_flight(existing, body_hash, key), None

    # Miss — open placeholder. Race-safe via the unique constraint:
    # two concurrent first-creators serialise here; the loser hits
    # IntegrityError and re-resolves the winner's row.
    record = IdempotencyKey(
        user=request.user,
        key=key,
        operation_name=operation_name,
        target_type=target_type,
        target_id=target_id,
        request_body_hash=body_hash,
        response_status=0,  # placeholder; record_response fills it.
        response_payload={},
        expires_at=timezone.now() + IdempotencyKey.DEFAULT_TTL,
    )
    try:
        record.save()
    except IntegrityError:
        existing = IdempotencyKey.objects.filter(
            user=request.user,
            operation_name=operation_name,
            key=key,
        ).first()
        if existing is None:
            # Integrity error without a row visible — not the race;
            # bubble up.
            raise
        return _existing_or_in_flight(existing, body_hash, key), None

    return None, record


def record_response(
    record: IdempotencyKey,
    status_code: int,
    payload: Any,
) -> None:
    """Persist the response onto the placeholder ``record``.

    Caller MUST invoke on every return path (success AND error) so
    error responses are also replay-safe (Stripe semantics). A
    placeholder left at ``response_status=0`` makes subsequent replays
    raise ``IdempotencyInFlight``.
    """
    record.response_status = status_code
    record.response_payload = payload if isinstance(payload, (dict, list)) else {}
    record.save(update_fields=["response_status", "response_payload"])
