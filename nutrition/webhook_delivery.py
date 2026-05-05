"""Webhook delivery for the nutrition outbox (DRF-306).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §5.1.

The delivery loop:
1. Pick a small batch of NutritionOutboxEvent rows where ``status == pending``
   AND (``next_retry_at IS NULL`` OR ``next_retry_at <= now``).
2. Sign each payload with HMAC-SHA256 using ``NUTRITION_WEBHOOK_SECRET``.
3. POST to ``NUTRITION_WEBHOOK_URL`` with idempotency header (event_id).
4. On 2xx → mark ``delivered``, stamp ``delivered_at``.
5. On non-2xx / network error → bump ``retry_count``, schedule
   ``next_retry_at`` via exponential backoff, save ``last_error``.
6. After ``MAX_RETRIES`` failed attempts → move row to ``dlq`` and log a
   warning so SRE / admin alerting picks it up.

Backoff schedule (seconds): 1 → 2 → 4 → 8 → 16. Total ladder ≤ 31s
plus the natural beat tick (10s default). DLQ kicks in on attempt 6.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone as dt_tz

import httpx
from django.conf import settings

from nutrition.models import NutritionOutboxEvent

logger = logging.getLogger(__name__)


MAX_RETRIES = 5
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
DELIVERY_BATCH = 50
HTTP_TIMEOUT_S = 10


def deliver_pending(*, batch_size: int = DELIVERY_BATCH) -> dict[str, int]:
    """Deliver a batch of pending rows. Returns counters for monitoring.

    Idempotent — receiver dedupes by ``X-Event-Id`` header (UUID4); a
    repeat delivery with the same id MUST be a no-op on their side per
    the contract.
    """
    url = getattr(settings, "NUTRITION_WEBHOOK_URL", "")
    if not url:
        return {"delivered": 0, "retried": 0, "dlq": 0, "skipped": 0}

    secret = getattr(settings, "NUTRITION_WEBHOOK_SECRET", "")
    now = datetime.now(dt_tz.utc)

    qs = (
        NutritionOutboxEvent.objects
        .filter(status=NutritionOutboxEvent.Status.PENDING)
        .filter(_eligible_for_delivery_filter(now))
        .order_by("created_at")[:batch_size]
    )
    rows = list(qs)
    if not rows:
        return {"delivered": 0, "retried": 0, "dlq": 0, "skipped": 0}

    delivered = retried = dlq = 0
    for row in rows:
        outcome = _attempt_one(row, url=url, secret=secret, now=now)
        if outcome == "delivered":
            delivered += 1
        elif outcome == "dlq":
            dlq += 1
        else:
            retried += 1

    return {"delivered": delivered, "retried": retried, "dlq": dlq, "skipped": 0}


def _eligible_for_delivery_filter(now: datetime):
    from django.db.models import Q
    return Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)


def _attempt_one(
    row: NutritionOutboxEvent,
    *,
    url: str,
    secret: str,
    now: datetime,
) -> str:
    body = {
        "event_id": str(row.id),
        "event_type": row.topic,
        "ts": row.created_at.isoformat(),
        "external_user_id": row.external_user_id,
        "payload": row.payload,
    }
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Event-Id": str(row.id),
        "X-Event-Type": row.topic,
    }
    if secret:
        headers["X-Signature"] = compute_signature(secret, body_bytes)

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.post(url, content=body_bytes, headers=headers)
        if 200 <= response.status_code < 300:
            row.status = NutritionOutboxEvent.Status.DELIVERED
            row.delivered_at = now
            row.last_error = ""
            row.save(update_fields=["status", "delivered_at", "last_error"])
            return "delivered"
        error = f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — never crash the worker on a single bad row
        error = f"{type(exc).__name__}: {exc}"

    return _record_failure(row, error=error, now=now)


def _record_failure(
    row: NutritionOutboxEvent, *, error: str, now: datetime,
) -> str:
    row.retry_count += 1
    row.last_error = error[:1000]

    if row.retry_count >= MAX_RETRIES:
        row.status = NutritionOutboxEvent.Status.DLQ
        row.next_retry_at = None
        row.save(update_fields=["status", "retry_count", "last_error", "next_retry_at"])
        logger.warning(
            "nutrition.webhook.dlq id=%s topic=%s retries=%d err=%s",
            row.id, row.topic, row.retry_count, row.last_error,
        )
        return "dlq"

    delay = BACKOFF_SECONDS[min(row.retry_count, len(BACKOFF_SECONDS)) - 1]
    row.next_retry_at = now + timedelta(seconds=delay)
    row.save(update_fields=["retry_count", "last_error", "next_retry_at"])
    logger.info(
        "nutrition.webhook.retry id=%s topic=%s retry=%d next_in=%ds err=%s",
        row.id, row.topic, row.retry_count, delay, row.last_error[:120],
    )
    return "retried"


def compute_signature(secret: str, body_bytes: bytes) -> str:
    """HMAC-SHA256 over the raw request body. Hex-encoded for header use."""
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"
