"""DRF-2306 — dead-letter outbox → сигнал операторам через бот.

Контракт бота §6.4 обещает оповещение о dead-letter; публикатор писал только
``logger.warning``. Теперь после батча — одно событие
``system.module.health.degraded`` (``module_name="appointments.outbox"``) на
(тема, класс), не чаще раза в UTC-час; бот поднимает страницу в MAX тем же
рельсом, что бюджет сканера (DRF-2196).

Узлы: dead по 4xx → ``rejected`` со slug причины из тела 422 бота; dead по
5xx на последней попытке → ``retries_exhausted``; несколько мёртвых одной
темы — один сигнал с числом, тот же час — без второго; смерть самого
системного события не сигналится (обрыв круга); в сигнале нет ни payload,
ни текста ошибки; сбой сигнала не ломает батч.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from appointments.infrastructure.outbox import publisher
from appointments.infrastructure.outbox.publisher import (
    MAX_DELIVERY_ATTEMPTS,
    publish_outbox_events_to_bot,
)
from appointments.models import OutboxEvent

SIGNAL_TOPIC = "system.module.health.degraded"
REJECTED_BODY = 'HTTP 422: {"status": "rejected", "reason": "tenant_not_found"}'


@pytest.fixture(autouse=True)
def _publisher_settings(settings):
    settings.BOT_PLATFORM_BASE_URL = "https://bot.test.local"
    settings.BOT_PLATFORM_INGEST_PATH = "/api/v1/internal/events/ingest"
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    cache.clear()
    yield
    cache.clear()


def _event(topic=OutboxEvent.Topic.BOOKING_CREATED, **kwargs) -> OutboxEvent:
    defaults = dict(
        topic=topic,
        payload={
            "event_id": "test",
            "event_name": str(topic),
            "data": {"client_name": "Анна Петрова", "phone": "+79991234567"},
        },
        external_delivery_enabled=True,
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


def _signals() -> list[OutboxEvent]:
    return list(OutboxEvent.objects.filter(topic=SIGNAL_TOPIC).order_by("created_at"))


def _publish(status: int | None, error: str):
    with patch.object(publisher, "_attempt_post", return_value=(status, error)):
        return publish_outbox_events_to_bot()


def _hour() -> str:
    return timezone.now().strftime("%Y-%m-%dT%H")


@pytest.mark.django_db
class TestDeadSignals:
    def test_a_rejection_signals_with_the_reason_slug(self) -> None:
        _event()
        summary = _publish(422, REJECTED_BODY)
        assert summary.dead == 1

        (signal,) = _signals()
        data = signal.payload["data"]
        assert data["module_name"] == "appointments.outbox"
        assert data["severity"] == "error"
        assert data["metric"] == {
            "topic": "booking.created",
            "failure": "rejected",
            "http_status": 422,
            "count": 1,
            "hour": _hour(),
            "reason": "tenant_not_found",
        }
        assert signal.payload["user_id"] is None
        assert signal.payload["tenant_id"] is None

    def test_exhausted_retries_signal_without_a_reason(self) -> None:
        _event(bot_attempt_count=MAX_DELIVERY_ATTEMPTS, bot_delivery_status="failed")
        assert _publish(500, "HTTP 500: boom").dead == 1

        (signal,) = _signals()
        metric = signal.payload["data"]["metric"]
        assert metric["failure"] == "retries_exhausted"
        assert metric["http_status"] == 500
        assert metric["reason"] is None

    def test_a_transient_failure_is_not_a_signal(self) -> None:
        """Контроль: 5xx до исчерпания попыток — повтор, не dead и не сигнал."""
        _event()
        assert _publish(500, "HTTP 500: boom").failed == 1
        assert _signals() == []  # empty-assert-ok: failed == 1 выше

    def test_one_signal_per_topic_class_and_hour(self) -> None:
        _event()
        _event()
        _publish(422, REJECTED_BODY)
        (signal,) = _signals()
        assert signal.payload["data"]["metric"]["count"] == 2

        _event()
        assert _publish(422, REJECTED_BODY).dead == 1
        assert len(_signals()) == 1  # тот же час — без второго сигнала

    def test_the_death_of_a_system_event_is_not_signalled(self) -> None:
        """Обрыв круга: сигнал о мёртвом сигнале не шлётся."""
        _event(topic=OutboxEvent.Topic.SYSTEM_MODULE_HEALTH_DEGRADED)
        assert _publish(422, REJECTED_BODY).dead == 1
        # Строка с этой темой одна — сама мёртвая; новой сигнальной не завелось.
        (only,) = _signals()
        assert only.bot_delivery_status == "dead"

    def test_no_payload_and_no_error_text_in_the_signal(self) -> None:
        _event()
        _publish(422, 'HTTP 422: {"reason": "Тенант Анна +79991234567"}')
        (signal,) = _signals()
        flat = str(signal.payload["data"])
        assert "appointments.outbox" in flat  # положительно: сигнал на месте
        assert "Анна" not in flat
        assert "79991234567" not in flat
        assert signal.payload["data"]["metric"]["reason"] is None  # не slug — не несём

    def test_a_signal_failure_does_not_break_the_batch(self) -> None:
        _event()
        with patch.object(publisher, "_emit_dead_signal", side_effect=RuntimeError("db")):
            summary = _publish(422, REJECTED_BODY)
        assert summary.dead == 1
        assert OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED).bot_delivery_status == "dead"
