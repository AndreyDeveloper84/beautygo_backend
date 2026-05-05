"""Helpers for enqueueing NutritionOutboxEvent rows from domain code (DRF-306).

The transactional-outbox guarantee requires the event row to be written
in the same DB transaction as the change that emitted it. Each helper
takes a pre-built payload dict, validates the topic, and inserts a
``pending`` row. The Celery delivery worker (nutrition.tasks) picks them
up and POSTs to the receiver.

Design choices:
- Helpers are thin: no defaulting of payload contents — the caller
  builds the right shape per topic. Keeps the contract close to the
  consumer (MAX bot's webhook handler).
- ``external_user_id`` is required on every event (the receiver scopes
  by it); we pull it from ``user.username`` when the caller passes a
  user object, otherwise the caller passes the string directly.
- Helpers are no-ops when ``settings.NUTRITION_WEBHOOK_URL`` is empty —
  Phase 3.4 backlog rollout flag. Pre-flag installs collect no rows.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from django.conf import settings

from nutrition.models import NutritionOutboxEvent

logger = logging.getLogger(__name__)


def enqueue_event(
    *,
    topic: str,
    external_user_id: str,
    payload: dict[str, Any],
    event_id: str | None = None,
) -> NutritionOutboxEvent | None:
    """Insert a pending outbox row inside the caller's transaction.

    Returns the created row, or ``None`` when the webhook is disabled
    via empty ``NUTRITION_WEBHOOK_URL`` setting.

    Idempotency: callers may pass ``event_id`` (UUID4 string) computed
    deterministically — the unique PK constraint then dedup retries
    that fire from the same domain operation. Without it we generate
    a fresh UUID per call, which is fine for one-shot triggers.
    """
    if not getattr(settings, "NUTRITION_WEBHOOK_URL", ""):
        return None

    if topic not in NutritionOutboxEvent.Topic.values:
        raise ValueError(f"Unknown nutrition outbox topic: {topic}")

    pk = event_id or str(uuid4())
    obj, created = NutritionOutboxEvent.objects.get_or_create(
        id=pk,
        defaults={
            "topic": topic,
            "external_user_id": external_user_id,
            "payload": payload,
        },
    )
    if not created:
        # Same event_id replayed — already enqueued, no-op. The worker
        # will deliver the existing row exactly once.
        logger.info(
            "nutrition.outbox.duplicate_event id=%s topic=%s",
            pk, topic,
        )
    return obj


def enqueue_profile_updated(*, external_user_id: str, profile_payload: dict) -> None:
    enqueue_event(
        topic=NutritionOutboxEvent.Topic.PROFILE_UPDATED,
        external_user_id=external_user_id,
        payload=profile_payload,
    )


def enqueue_water_logged(*, external_user_id: str, entry_payload: dict) -> None:
    enqueue_event(
        topic=NutritionOutboxEvent.Topic.WATER_LOGGED,
        external_user_id=external_user_id,
        payload=entry_payload,
    )


def enqueue_milestone_reached(
    *, external_user_id: str, threshold: int, day: str, today_total_water_ml: int,
) -> None:
    enqueue_event(
        topic=NutritionOutboxEvent.Topic.MILESTONE_REACHED,
        external_user_id=external_user_id,
        payload={
            "threshold_pct": threshold,
            "day": day,
            "today_total_water_ml": today_total_water_ml,
        },
    )


def enqueue_pattern_detected(
    *, external_user_id: str, pattern_slug: str, severity: str, count: int,
) -> None:
    enqueue_event(
        topic=NutritionOutboxEvent.Topic.PATTERN_DETECTED,
        external_user_id=external_user_id,
        payload={
            "pattern_slug": pattern_slug,
            "severity": severity,
            "count": count,
        },
    )


def enqueue_recognition_completed(
    *, external_user_id: str, scan_id: str, dish_name: str, confidence: float,
) -> None:
    enqueue_event(
        topic=NutritionOutboxEvent.Topic.RECOGNITION_COMPLETED,
        external_user_id=external_user_id,
        payload={
            "scan_id": scan_id,
            "dish_name": dish_name,
            "confidence": confidence,
        },
    )
