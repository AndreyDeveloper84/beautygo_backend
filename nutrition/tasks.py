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


#: Сколько просроченных сканов снимается за один прогон. На пилоте их
#: пятнадцать; предел — чтобы разрыв хранилища не превратил суточный тик
#: в многочасовой.
FOOD_PHOTO_PURGE_BATCH = 500


@shared_task(name="nutrition.purge_expired_food_photos")
def purge_expired_food_photos_task() -> dict:
    """§134/§135 по расписанию (DRF-1843): фотографии еды старше 30 суток.

    Команда ``purge_expired_food_photos`` была, запускать её было некому:
    срок объявлен, но не исполнялся ничем, а §134 требует, чтобы удаление
    подтверждалось событием аудита либо метрикой и чтобы система выявляла
    не удалившиеся в срок.

    Каждый прогон, включён флаг или нет, пишет ``AnalyticsEvent``
    ``food_photo_purge_run`` с числами — хранимое событие переживает
    выкладку, лог контейнера нет. ``expired_after`` — число §134 «не
    удалено в срок»: при выключенном флаге оно равно ``expired_before``,
    и это видно, а не молчит.

    Удаление — только при ``FOOD_PHOTO_PURGE_ENABLED``: необратимо, и
    включение на пилоте — слово владельца. Порядок и пять исходов — те же,
    что у команды (``purge_one``).
    """
    import uuid
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    from analytics import event_catalogue
    from nutrition.management.commands.purge_expired_food_photos import (
        TTL_DAYS,
        Tally,
        purge_one,
    )
    from nutrition.models import FoodScan

    enabled = bool(getattr(settings, "FOOD_PHOTO_PURGE_ENABLED", False))
    cutoff = timezone.now() - timedelta(days=TTL_DAYS)
    expired = FoodScan.objects.filter(created_at__lt=cutoff).order_by("created_at")
    expired_before = expired.count()

    tally = Tally()
    if enabled:
        for scan in list(expired[:FOOD_PHOTO_PURGE_BATCH]):
            outcome, reason = purge_one(scan)
            if outcome == "deleted":
                tally.deleted += 1
            elif outcome == "no_image":
                tally.no_image += 1
            elif outcome == "object_absent":
                tally.object_absent += 1
            else:
                tally.refused.append((scan.id, reason))

    expired_after = FoodScan.objects.filter(created_at__lt=cutoff).count()
    result = {
        "enabled": enabled,
        "ttl_days": TTL_DAYS,
        "expired_before": expired_before,
        "expired_after": expired_after,
        "deleted": tally.deleted,
        "object_absent": tally.object_absent,
        "no_image": tally.no_image,
        "refused": len(tally.refused),
    }

    try:
        from analytics.models import AnalyticsEvent

        AnalyticsEvent.objects.create(
            event_name=event_catalogue.FOOD_PHOTO_PURGE_RUN,
            payload=result,
            app_type=AnalyticsEvent.AppType.CLIENT,
            client_event_id=uuid.uuid4(),
        )
    except Exception:  # noqa: BLE001 — the purge must not be undone by its receipt
        logger.exception("nutrition.food_photo_purge.event_write_failed")

    log = logger.warning if expired_after else logger.info
    log(
        "nutrition.food_photo_purge enabled=%s expired_before=%d expired_after=%d "
        "deleted=%d object_absent=%d no_image=%d refused=%d",
        enabled,
        expired_before,
        expired_after,
        tally.deleted,
        tally.object_absent,
        tally.no_image,
        len(tally.refused),
    )
    return result
