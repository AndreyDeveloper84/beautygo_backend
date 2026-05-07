"""Celery tasks for the users app.

Today: ``infer_user_patterns`` — daily UserPersonalContext refresh
from booking history (DRF-230 PR 2).
"""
from __future__ import annotations

import logging

from celery import shared_task

from users.services.personal_context_inference import (
    infer_for_active_users,
    infer_for_user,
)


logger = logging.getLogger(__name__)


@shared_task(name="users.infer_user_patterns")
def infer_user_patterns() -> dict[str, int]:
    """Nightly UserPersonalContext refresh — see service module.

    Idempotent. Safe to run multiple times per day. Skips fields the
    user has explicitly typed (data_sources says ``"explicit"``) so
    inference never overrides intent.

    Returns a counters dict the beat scheduler logs aggregate.
    """
    counters = infer_for_active_users()
    logger.info(
        "users.infer_user_patterns.complete users=%d failures=%d "
        "favs=%d busy=%d",
        counters["processed_users"],
        counters["failed_users"],
        counters["favorite_masters_inferred"],
        counters["busy_days_inferred"],
    )
    return counters


@shared_task(name="users.infer_user_patterns_for_one")
def infer_user_patterns_for_one(user_id: str) -> dict:
    """Single-user variant — useful for warmup runs and admin "force
    refresh" actions. Same idempotency guarantees as the full pass.
    """
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        logger.warning(
            "users.infer_user_patterns_for_one.user_missing user=%s", user_id,
        )
        return {"error": "user_missing"}
    outcome = infer_for_user(user)
    return {
        "user_id": outcome.user_id,
        "favs_inferred": len(outcome.favorite_masters_added),
        "busy_inferred": len(outcome.busy_days_added),
        "skipped_explicit": outcome.skipped_explicit,
    }
