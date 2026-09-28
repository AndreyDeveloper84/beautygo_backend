"""Задачи приложения services по расписанию."""

from __future__ import annotations

import logging
import uuid

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="services.purge_expired_mapping_reports")
def purge_expired_mapping_reports_task() -> dict:
    """Отчёты разбора услуг старше срока — по расписанию (DRF-2409).

    Решение владельца 28.09 (п.5): «Храним отчёты 90 дней с последнего прогона
    по конкретному салону». Срок считается от ``generated_at`` внутри отчёта
    салона (``services.mapping.store``), не от даты файла и не глобально.

    Каждый прогон, включён флаг или нет, пишет ``AnalyticsEvent``
    ``mapping_report_purge_run`` с числами: событие переживает выкладку, лог
    контейнера нет. Без флага ``expired`` показывает, сколько отчётов уже
    старше срока и лежит, — это видно, а не молчит.

    Удаление — только при ``MAPPING_REPORT_PURGE_ENABLED``: необратимо, и
    включение на стенде — слово владельца. Тот же образец, что у
    ``nutrition.purge_expired_food_photos``: команда без расписания там уже
    была, и «запускать её было некому».
    """
    from django.conf import settings

    from analytics import event_catalogue
    from services.mapping.store import purge_expired_reports

    enabled = bool(getattr(settings, "MAPPING_REPORT_PURGE_ENABLED", False))
    result = purge_expired_reports(apply=enabled)
    counts = {
        "enabled": enabled,
        "retention_days": result["retention_days"],
        "dir_exists": result["dir_exists"],
        "examined": result["examined"],
        "expired": result["expired"],
        "deleted": result["deleted"],
        "refused": result["refused"],
        "unreadable": result["unreadable"],
    }

    try:
        from analytics.models import AnalyticsEvent

        AnalyticsEvent.objects.create(
            event_name=event_catalogue.MAPPING_REPORT_PURGE_RUN,
            payload=counts,
            app_type=AnalyticsEvent.AppType.CLIENT,
            client_event_id=uuid.uuid4(),
        )
    except Exception:  # noqa: BLE001 — удаление не отменяется из-за квитанции
        logger.exception("services.mapping_report_purge.event_write_failed")

    left = counts["expired"] - counts["deleted"]
    log = logger.warning if (left or counts["unreadable"] or not counts["dir_exists"]) else logger.info
    log(
        "services.mapping_report_purge enabled=%s dir_exists=%s examined=%d expired=%d "
        "deleted=%d refused=%d unreadable=%d",
        enabled,
        counts["dir_exists"],
        counts["examined"],
        counts["expired"],
        counts["deleted"],
        counts["refused"],
        counts["unreadable"],
    )
    return counts
