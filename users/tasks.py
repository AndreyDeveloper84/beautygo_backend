"""Celery tasks for the users app.

Today: ``infer_user_patterns`` — daily UserPersonalContext refresh
from booking history (DRF-230 PR 2).
"""
from __future__ import annotations

import logging

from celery import shared_task

from users.personal_context_inference import (
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


# ---------------------------------------------------------------------------
# D3 — исполнитель удаления аккаунта (§7, DRF-1725)
# ---------------------------------------------------------------------------


@shared_task(name="users.execute_deletion_requests")
def execute_deletion_requests(limit: int = 20) -> dict[str, int]:
    """Тик исполнителя: взять открытые заявки (REQUESTED / PROCESSING /
    FAILED — повтор по той же записи), у которых прошло окно
    ``DELETION_GRACE_DAYS`` с приёма, и исполнить по одной.

    Идемпотентно: заявка, у которой каталог уже стёрт, а бот не подтвердил,
    снова спросит только бота. Сбой одной заявки не останавливает остальные.
    """
    from users.deletion_executor import GraceMisconfigured, execute, open_requests_due

    counters = {"scanned": 0, "completed": 0, "open": 0}
    try:
        due = list(open_requests_due()[:limit])
    except GraceMisconfigured:
        # Кривое окно — не «исполнить всё сразу»: тик не берёт никого и
        # говорит об этом громко.
        logger.exception("users.execute_deletion_requests.grace_misconfigured — nothing taken")
        return counters
    for req in due:
        counters["scanned"] += 1
        try:
            outcome = execute(req)
        except Exception:  # noqa: BLE001 — одна заявка не валит тик
            logger.exception("users.execute_deletion_requests.crashed request=%s", req.pk)
            counters["open"] += 1
            continue
        counters["completed" if outcome.completed else "open"] += 1
    logger.info("users.execute_deletion_requests.tick %s", counters)
    return counters


@shared_task(name="users.execute_deletion_request")
def execute_deletion_request(request_id: str) -> dict:
    """Одна заявка по номеру — для admin-действия «исполнить сейчас»."""
    from users.deletion_executor import execute
    from users.models import DeletionRequest

    req = DeletionRequest.objects.filter(pk=request_id).first()
    if req is None:
        return {"error": "request_missing"}
    outcome = execute(req)
    return {"request_id": outcome.request_id, "status": outcome.status,
            "failure_reason": outcome.failure_reason}
