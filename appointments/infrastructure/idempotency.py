"""Idempotency helper for mutating booking endpoints (#512).

Usage in a view::

    from appointments.infrastructure.idempotency import (
        IdempotencyConflict, lookup_or_open_idempotency, record_response,
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
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        # ... execute the operation, get a response ...
        response = success_response(...)
        if record is not None:
            record_response(record, response.status_code, response.data)
        return response

Contract:
- ``lookup_or_open_idempotency`` returns ``(cached_response, record)``
  - ``(None, None)`` — caller did not supply X-Idempotency-Key. Pass
    through with no tracking.
  - ``(cached_response, None)`` — replay hit; return the cached response.
    ``cached_response`` is a dict with keys ``status`` and ``payload``.
  - ``(None, record)`` — fresh key; caller MUST call ``record_response``
    after executing the operation so the next replay can return cached.
- Raises ``IdempotencyConflict`` when the same key is replayed with a
  different normalised body — that's a client bug, not a retry.

Body normalisation:
- ``json.dumps(body, sort_keys=True, separators=(',', ':'))`` then SHA256.
- Stable across whitespace, key order, and float vs int representation
  (JSON-compatible inputs).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from django.utils import timezone

from appointments.models import IdempotencyKey


class IdempotencyConflict(Exception):
    """X-Idempotency-Key reused with a different request body."""


def _hash_body(body: Any) -> str:
    """SHA256 of the JSON-canonical form of ``body``."""
    if body is None:
        body = {}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup_or_open_idempotency(
    request,
    *,
    operation_name: str,
    target_type: str = "",
    target_id: str = "",
) -> tuple[dict | None, IdempotencyKey | None]:
    """Look up the (user, operation, key) tuple from X-Idempotency-Key.

    Returns ``(cached, record)`` as documented in the module docstring.
    """
    key = (request.META.get("HTTP_X_IDEMPOTENCY_KEY") or "").strip()
    if not key:
        # No header → no tracking. Caller proceeds without idempotency.
        return None, None

    body_hash = _hash_body(getattr(request, "data", {}) or {})
    existing = IdempotencyKey.objects.filter(
        user=request.user,
        operation_name=operation_name,
        key=key,
    ).first()

    if existing is not None:
        # TTL check — expired keys are treated as cache miss; the
        # cleanup task will prune them, but a request that arrives
        # between expiry and prune should re-execute.
        if existing.expires_at <= timezone.now():
            existing.delete()
        else:
            if existing.request_body_hash != body_hash:
                raise IdempotencyConflict(
                    f"X-Idempotency-Key '{key}' reused with a different body."
                )
            return (
                {
                    "status": existing.response_status,
                    "payload": existing.response_payload,
                },
                None,
            )

    # Miss — open a placeholder. The unique constraint on
    # (user, operation_name, key) means two concurrent requests
    # with the same key serialise here: the second hits IntegrityError
    # on save, which the caller must catch as a race signal.
    record = IdempotencyKey(
        user=request.user,
        key=key,
        operation_name=operation_name,
        target_type=target_type,
        target_id=target_id,
        request_body_hash=body_hash,
        # Placeholder values overwritten by ``record_response``.
        response_status=0,
        response_payload={},
        expires_at=timezone.now() + IdempotencyKey.DEFAULT_TTL,
    )
    record.save()
    return None, record


def record_response(record: IdempotencyKey, status_code: int, payload: Any) -> None:
    """Persist the response onto the placeholder ``record``.

    Called after the operation succeeds. Subsequent replays of the same
    key return ``(status_code, payload)`` until ``expires_at``.
    """
    record.response_status = status_code
    record.response_payload = payload if isinstance(payload, (dict, list)) else {}
    record.save(update_fields=["response_status", "response_payload"])
