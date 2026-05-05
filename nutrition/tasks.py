"""Celery tasks for the nutrition app.

Currently a single periodic task — the WaterEntry soft-delete purge
mandated by DRF-302 acceptance ("Daily Celery purge >90 дней"). When
more nutrition jobs land they belong here rather than scattered across
service modules.
"""
from __future__ import annotations

import logging

from celery import shared_task

from nutrition.services.water_entry_service import purge_deleted_water_entries
from nutrition.webhook_delivery import deliver_pending

logger = logging.getLogger(__name__)


@shared_task(name="nutrition.purge_deleted_water_entries")
def purge_deleted_water_entries_task(older_than_days: int = 90) -> int:
    """Hard-delete WaterEntry rows soft-deleted more than N days ago.

    Run daily via Celery beat. Returns the row count purged so the
    monitoring dashboard can graph cleanup volume over time. Idempotent —
    re-running the same day is a cheap empty-queryset delete.
    """
    purged = purge_deleted_water_entries(older_than_days=older_than_days)
    if purged:
        logger.info(
            "nutrition.purge_water_entries purged=%d older_than_days=%d",
            purged, older_than_days,
        )
    return purged


@shared_task(name="nutrition.deliver_outbox_events")
def deliver_outbox_events_task() -> dict:
    """Periodic webhook delivery for NutritionOutboxEvent (DRF-306).

    No-op when ``NUTRITION_WEBHOOK_URL`` is empty (rollout flag). Runs
    every 10 seconds via Celery beat alongside the appointments outbox
    dispatcher — same cadence keeps cross-system events near-real-time
    without spinning up a dedicated worker.
    """
    return deliver_pending()
