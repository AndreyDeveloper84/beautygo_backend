"""Cross-service HTTP publisher for ``OutboxEvent`` rows (Block C → C2 + C4).

The legacy dispatcher in :mod:`appointments.tasks` routes events to
in-process handlers (notifications, log stubs). This module ships
the second half of the dual-delivery contract: posting the same
ADR-0009 envelope to bot-platform's ``/api/v1/internal/events/ingest``
endpoint so cross-service consumers (RemoteBookingProxy, reminder
FSM, RFM scoring, conversation memory) observe Ayla state changes.

Closes codex P0-4 (outbox events created but not delivered).

Architecture
------------

The dual-delivery schema landed in migration ``0010`` (C1). A row is
visible to this publisher iff::

    external_delivery_enabled = True
    AND bot_delivery_status IN ('pending', 'failed')
    AND (bot_next_retry_at IS NULL OR bot_next_retry_at <= now())

The publisher walks rows in ``created_at`` order, POSTs each, and
updates the per-row delivery fields atomically. Three outcomes:

* **2xx** → ``bot_delivery_status='sent'``, ``bot_delivered_at=now()``,
  ``bot_response_status=status_code``. (Future ``acknowledged`` state
  arrives if bot-platform implements async acks — out of scope for C2.)
* **5xx / network / timeout** → ``bot_delivery_status='failed'``,
  ``bot_attempt_count += 1``, ``bot_next_retry_at`` set per the
  backoff curve (C4), ``bot_last_error`` captures the exception.
  When ``bot_attempt_count`` reaches :data:`MAX_DELIVERY_ATTEMPTS`,
  the row transitions to ``dead`` instead.
* **4xx (except 429)** → permanent client bug. Immediate dead-letter:
  ``bot_delivery_status='dead'``, ``bot_dead_lettered_at=now()``.
  ``429`` is treated as transient (rate limited) and follows the
  retry path.

Backoff curve (C4)
------------------

``bot_next_retry_at = now() + min(2^attempt_count * 30s, 1h)``.

Worked examples:

* attempt 1 → +60s
* attempt 2 → +2m
* attempt 3 → +4m
* attempt 4 → +8m
* attempt 5 → +16m
* attempt 6 → +32m
* attempt 7 → +1h (cap)
* attempt 8 → +1h
* attempt 9 → dead-letter (8 retries exhausted)

Total elapsed before dead-letter ≈ 4.5h. Operators replay via
``manage.py replay_dead_outbox_events`` (C5).

Idempotency
-----------

The HTTP request carries ``X-Idempotency-Key`` set to the
``OutboxEvent.id`` (UUID). Bot-platform's ``IngestDedupe`` table
guarantees the consumer side-effect runs once regardless of how
many retries we issue. The envelope's ``event_id`` field carries
the same value as a payload-level dedupe key for any consumer that
prefers it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from appointments.models import OutboxEvent

logger = logging.getLogger(__name__)


# --- tunables ---------------------------------------------------------

# Per-tick batch size — outbox queue is usually small; this caps work
# on one tick so a backlog spike does not hold the worker open. Bot
# ingest scales horizontally so the per-tick fan-out is the real
# bottleneck, not the consumer.
BATCH_SIZE: int = 50

# Maximum transient-failure attempts before dead-letter. After this
# many failures the row stops being retried and operators must replay
# via the C5 management command.
MAX_DELIVERY_ATTEMPTS: int = 8

# Backoff curve constants. ``2^attempt * 30s`` capped at 1h gives
# ~4.5h total elapsed before dead-letter at attempt 9.
BACKOFF_BASE_SECONDS: int = 30
BACKOFF_CAP_SECONDS: int = 60 * 60  # 1h

# HTTP timeout for the ingest POST. The bot ingest endpoint is
# expected to ack within seconds; longer than 10s indicates the bot
# is overloaded or unreachable and we should treat it as a transient
# failure to retry.
HTTP_TIMEOUT_SECONDS: float = 10.0


# --- result objects ---------------------------------------------------


@dataclass(frozen=True)
class PublishOutcome:
    """Outcome of a single delivery attempt — what to write back to the row."""

    status: str  # one of: 'sent', 'failed', 'dead'
    http_status: int | None  # HTTP status code if a response was received
    error: str  # populated on failed/dead
    delivered_at: datetime | None = None
    dead_lettered_at: datetime | None = None
    next_retry_at: datetime | None = None


@dataclass
class PublishBatchSummary:
    """Aggregated counters returned by ``publish_outbox_events_to_bot``."""

    sent: int = 0
    failed: int = 0
    dead: int = 0
    scanned: int = 0


# --- public API -------------------------------------------------------


def _ingest_url() -> str:
    """Resolve the bot-platform ingest URL from settings.

    Reads ``BOT_PLATFORM_BASE_URL`` + ``BOT_PLATFORM_INGEST_PATH``.
    Raises ``RuntimeError`` if base is unset — the publisher must
    not silently send nowhere.
    """
    base: str = getattr(settings, "BOT_PLATFORM_BASE_URL", "") or ""
    if not base:
        raise RuntimeError(
            "BOT_PLATFORM_BASE_URL is unset. The outbox publisher cannot "
            "ship to bot-platform without a target host. Set the env in "
            "prod / staging deploys; locally leave it empty so the "
            "publisher no-ops until configured."
        )
    path: str = getattr(
        settings, "BOT_PLATFORM_INGEST_PATH", "/api/v1/internal/events/ingest",
    )
    return base.rstrip("/") + path


def _bearer_token() -> str:
    token: str = getattr(settings, "AYLA_INTERNAL_API_TOKEN", "") or ""
    if not token:
        # Prod boot already fails on this (A5). Belt-and-suspenders here
        # so a unit-test env that wipes the setting after import gets a
        # clear message instead of a 401 from bot.
        raise RuntimeError(
            "AYLA_INTERNAL_API_TOKEN is unset. The publisher cannot "
            "authenticate to bot-platform without it."
        )
    return token


def _backoff_seconds(attempt: int) -> int:
    """C4 backoff curve.

    ``attempt`` is the post-increment counter — attempt 1 = first
    retry after the initial failure. Returns seconds until the next
    eligible delivery time.
    """
    exp = BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return min(exp, BACKOFF_CAP_SECONDS)


def _attempt_post(envelope: dict, event_id) -> tuple[int | None, str]:
    """Issue one POST. Returns ``(http_status, error_msg)``.

    ``http_status`` is ``None`` only when no response was received
    (timeout / connection error). ``error_msg`` is empty on 2xx and
    populated for any failure path so the caller can record it.
    """
    url = _ingest_url()
    headers = {
        "Authorization": f"Bearer {_bearer_token()}",
        "Content-Type": "application/json",
        # Idempotency key = event_id (UUID). Bot's IngestDedupe table
        # uses this so a retry from us is a no-op on the consumer side.
        "X-Idempotency-Key": str(event_id),
        # User-Agent helps ops triage between client traffic and
        # internal cross-service traffic in bot-platform access logs.
        "User-Agent": "ayla-outbox-publisher/1",
    }
    try:
        response = requests.post(
            url, json=envelope, headers=headers, timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        return None, f"Timeout: {exc}"
    except requests.ConnectionError as exc:
        return None, f"ConnectionError: {exc}"
    except requests.RequestException as exc:
        # Catch-all for any requests-layer failure — DNS issues, SSL
        # errors, malformed responses. Treat as transient.
        return None, f"{exc.__class__.__name__}: {exc}"

    if 200 <= response.status_code < 300:
        return response.status_code, ""
    return response.status_code, f"HTTP {response.status_code}: {response.text[:200]}"


def _classify(http_status: int | None, attempt_count: int) -> str:
    """Decide the next ``bot_delivery_status`` based on the response.

    * 2xx → ``sent`` (caller marks delivered_at)
    * 429 / 5xx / no-response → ``failed`` unless this attempt
      exhausts the retry budget, in which case → ``dead``.
    * 4xx other than 429 → ``dead`` immediately (client bug, no
      benefit in retrying)
    """
    if http_status is not None and 200 <= http_status < 300:
        return "sent"
    if http_status is not None and 400 <= http_status < 500 and http_status != 429:
        return "dead"  # permanent client failure (auth, validation)
    # 429 / 5xx / no-response → transient. Becomes dead only after
    # the attempt counter exhausts the retry budget.
    if attempt_count >= MAX_DELIVERY_ATTEMPTS:
        return "dead"
    return "failed"


def _build_outcome(
    http_status: int | None, error: str, attempt_count_after: int,
) -> PublishOutcome:
    """Translate a raw HTTP attempt into a row-update outcome.

    ``attempt_count_after`` is the post-increment counter (so the
    classifier can decide whether we just exhausted the retry budget).
    """
    status = _classify(http_status, attempt_count_after)
    now = timezone.now()
    if status == "sent":
        return PublishOutcome(
            status="sent",
            http_status=http_status,
            error="",
            delivered_at=now,
        )
    if status == "dead":
        return PublishOutcome(
            status="dead",
            http_status=http_status,
            error=error or f"dead-lettered after {attempt_count_after} attempts",
            dead_lettered_at=now,
        )
    # transient — schedule next retry
    return PublishOutcome(
        status="failed",
        http_status=http_status,
        error=error or "unknown transient failure",
        next_retry_at=now + timedelta(seconds=_backoff_seconds(attempt_count_after)),
    )


def deliver_event(event: "OutboxEvent") -> PublishOutcome:
    """Issue one delivery attempt for a single event row.

    Pure function over network state — does not mutate the row.
    The caller (``_apply_outcome``) writes the result back inside
    its transaction.
    """
    envelope = event.payload if isinstance(event.payload, dict) else {}
    # Per-row attempt counter is the count BEFORE this attempt; the
    # outcome uses the post-increment value so dead-letter classification
    # is correct on the attempt that hits the limit.
    attempt_after = (event.bot_attempt_count or 0) + 1
    http_status, error = _attempt_post(envelope, event.id)
    return _build_outcome(http_status, error, attempt_after)


def _apply_outcome(event: "OutboxEvent", outcome: PublishOutcome) -> None:
    """Persist the delivery outcome onto the row.

    Caller MUST hold a ``select_for_update`` lock on the row (the
    batch loop does this). Updates only the fields touched by this
    outcome — using ``update_fields`` minimises the per-row write.
    """
    fields = ["bot_delivery_status", "bot_attempt_count", "bot_response_status"]
    event.bot_delivery_status = outcome.status
    event.bot_attempt_count = (event.bot_attempt_count or 0) + 1
    event.bot_response_status = outcome.http_status

    if outcome.error:
        event.bot_last_error = outcome.error
        fields.append("bot_last_error")
    if outcome.delivered_at is not None:
        event.bot_delivered_at = outcome.delivered_at
        fields.append("bot_delivered_at")
    if outcome.dead_lettered_at is not None:
        event.bot_dead_lettered_at = outcome.dead_lettered_at
        fields.append("bot_dead_lettered_at")
    if outcome.next_retry_at is not None:
        event.bot_next_retry_at = outcome.next_retry_at
        fields.append("bot_next_retry_at")
    event.save(update_fields=fields)


def publish_outbox_events_to_bot() -> PublishBatchSummary:
    """Walk the publisher-eligible queue and deliver one batch.

    Called by the Celery beat schedule (see ``settings/base.py``).
    Always processes at most :data:`BATCH_SIZE` rows per tick.

    Returns a summary dict (sent / failed / dead / scanned) for
    observability — Celery's result backend stores it for monitoring.
    """
    from appointments.models import OutboxEvent

    summary = PublishBatchSummary()
    now = timezone.now()

    # Eligibility filter mirrors the composite index
    # ``outbox_publisher_scan_idx`` from migration 0010 so the planner
    # can short-circuit without scanning rows that opted out of
    # external delivery. Use ``Q(... ) | Q(...)`` rather than
    # ``qs1 | qs2`` so the result is a single SELECT (PostgreSQL
    # cannot apply FOR UPDATE to a UNION query).
    base_qs = OutboxEvent.objects.filter(
        external_delivery_enabled=True,
        bot_delivery_status__in=["pending", "failed"],
    ).filter(
        Q(bot_next_retry_at__isnull=True) | Q(bot_next_retry_at__lte=now),
    )

    with transaction.atomic():
        rows = list(
            base_qs.select_for_update(skip_locked=True)
            .order_by("created_at")[:BATCH_SIZE]
        )
        summary.scanned = len(rows)

        for event in rows:
            outcome = deliver_event(event)
            _apply_outcome(event, outcome)
            if outcome.status == "sent":
                summary.sent += 1
            elif outcome.status == "dead":
                summary.dead += 1
                logger.warning(
                    "outbox.publisher.dead_lettered event_id=%s topic=%s "
                    "attempts=%d last_error=%s",
                    event.id, event.topic, event.bot_attempt_count,
                    event.bot_last_error,
                )
            else:
                summary.failed += 1

    if summary.scanned:
        logger.info(
            "outbox.publisher.batch sent=%d failed=%d dead=%d scanned=%d",
            summary.sent, summary.failed, summary.dead, summary.scanned,
        )
    return summary
