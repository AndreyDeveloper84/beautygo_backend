"""DRF-2525 — мёртвые письма каталога видны в ``/api/v1/health/ready/``.

Шесть ``booking.completed`` умерли в ящике с 24.08 по 09.09, и две недели
этого никто не заметил: ни один доступный без shell ответ не показывал
мёртвых писем. Readiness — существующая внутренняя ручка, которую главное окно
читает живым HTTP, поэтому число едет туда, а не в новую поверхность.

Что держат узлы:

* **охват рядом с числом.** Ноль мёртвых без охвата значил бы и «всё дошло», и
  «смотрел не туда». Поэтому в ответе есть ``rows_seen`` и окно ``created_at``;
* **подмена.** Мёртвое письмо положено → число и тема видны; до подмены ноль;
* **статус не зависит от мёртвых писем.** Мёртвое письмо — повод человеку
  посмотреть, а не повод выводить машину из ротации и ронять скрипт выкладки;
* **ни идентификаторов, ни полезной нагрузки.** Ручка открыта без авторизации;
* **ошибка подсчёта не роняет readiness.**
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import OutboxEvent
from djangoProject.health import OUTBOX_CACHE_KEY, OUTBOX_CACHE_TTL_S

READY_URL = "/api/v1/health/ready/"
SECRET_MARKER = "payload-must-not-leak"


@pytest.fixture
def anon():
    return APIClient()


@pytest.fixture(autouse=True)
def _fresh_outbox_cache():
    # Подсчёт кэшируется на минуту; узлы не должны видеть число соседа.
    cache.delete(OUTBOX_CACHE_KEY)
    yield
    cache.delete(OUTBOX_CACHE_KEY)


def _row(topic: str, status: str, **kwargs) -> OutboxEvent:
    defaults = dict(
        topic=topic,
        payload={"event_id": "x", "tenant_id": "t-1", "data": {"note": SECRET_MARKER}},
        external_delivery_enabled=True,
        bot_delivery_status=status,
    )
    if status == OutboxEvent.BotDeliveryStatus.DEAD:
        defaults.update(
            bot_dead_lettered_at=timezone.now() - timedelta(days=3),
            bot_attempt_count=8,
            bot_last_error="HTTP 500",
        )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


def _outbox(anon) -> dict:
    response = anon.get(READY_URL)
    assert response.status_code == 200, response.content
    return response.json()["outbox"]


@pytest.mark.django_db
class TestDeadLettersVisible:
    def test_dead_counted_by_topic(self, anon):
        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        _row(OutboxEvent.Topic.BOOKING_CREATED, OutboxEvent.BotDeliveryStatus.DEAD)
        _row(OutboxEvent.Topic.BOOKING_CREATED, OutboxEvent.BotDeliveryStatus.SENT)

        outbox = _outbox(anon)

        assert outbox["dead_total"] == 3
        assert outbox["dead_by_topic"] == {"booking.completed": 2, "booking.created": 1}
        assert outbox["oldest_dead_at"] is not None

    def test_substitution_zero_then_one(self, anon):
        # Подмена: без мёртвых — ноль при НЕПУСТОМ охвате; положили одно — видно.
        _row(OutboxEvent.Topic.BOOKING_CREATED, OutboxEvent.BotDeliveryStatus.SENT)
        before = _outbox(anon)
        assert before["rows_seen"] == 1
        assert before["dead_total"] == 0
        assert before["dead_by_topic"] == {}

        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        cache.delete(OUTBOX_CACHE_KEY)  # иначе второй опрос отдаст число первого
        after = _outbox(anon)
        assert after["dead_total"] == 1
        assert after["dead_by_topic"] == {"booking.completed": 1}


@pytest.mark.django_db
class TestPriceIsBounded:
    def test_second_poll_within_ttl_does_not_recount(self, anon):
        # CI и скрипты выкладки опрашивают readiness часто — проход по ящику
        # должен быть не чаще раза в TTL, а ответ говорит, когда число снято.
        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        first = _outbox(anon)
        with patch(
            "djangoProject.health.OutboxEvent.objects.values",
            side_effect=AssertionError("recounted within TTL"),
        ):
            second = _outbox(anon)
        assert second == first
        assert first["cache_ttl_s"] == OUTBOX_CACHE_TTL_S
        assert isinstance(first["computed_at"], int)

    def test_error_is_not_cached(self, anon):
        with patch(
            "djangoProject.health.OutboxEvent.objects.values",
            side_effect=RuntimeError("boom"),
        ):
            assert _outbox(anon) == {"error": "RuntimeError"}
        # Следующий опрос пробует заново, а не повторяет ошибку минуту.
        assert "rows_seen" in _outbox(anon)


@pytest.mark.django_db
class TestCoverageNextToNumber:
    def test_rows_seen_window_and_status_breakdown(self, anon):
        _row(OutboxEvent.Topic.BOOKING_CREATED, OutboxEvent.BotDeliveryStatus.SENT)
        _row(OutboxEvent.Topic.BOOKING_CREATED, OutboxEvent.BotDeliveryStatus.PENDING)
        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)

        outbox = _outbox(anon)

        assert outbox["rows_seen"] == 3
        assert outbox["by_delivery_status"] == {"sent": 1, "pending": 1, "dead": 1}
        assert outbox["window"]["from"] is not None
        assert outbox["window"]["to"] is not None
        assert outbox["window"]["from"] <= outbox["window"]["to"]

    def test_empty_outbox_says_so(self, anon):
        # Пустой ящик — ноль мёртвых НИЧЕГО не доказывает, и ответ это видно:
        # rows_seen = 0, окна нет.
        outbox = _outbox(anon)
        assert outbox["rows_seen"] == 0
        assert outbox["dead_total"] == 0
        assert outbox["window"] == {"from": None, "to": None}


@pytest.mark.django_db
class TestDoesNotChangeReadiness:
    def test_dead_letters_do_not_flip_status(self, anon):
        _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        response = anon.get(READY_URL)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        # И не прячется среди проверок, от которых зависит статус.
        assert "outbox" not in response.json()["checks"]

    def test_count_error_does_not_break_readiness(self, anon):
        with patch(
            "djangoProject.health.OutboxEvent.objects.values",
            side_effect=RuntimeError("boom"),
        ):
            response = anon.get(READY_URL)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["outbox"] == {"error": "RuntimeError"}

    def test_liveness_untouched(self, anon):
        # Liveness дёргается балансировщиком часто — подсчёт туда не едет.
        assert "outbox" not in anon.get("/api/v1/health/").json()


@pytest.mark.django_db
class TestNoIdsNoPayload:
    def test_response_carries_no_ids_or_payload(self, anon):
        row = _row(OutboxEvent.Topic.BOOKING_COMPLETED, OutboxEvent.BotDeliveryStatus.DEAD)
        raw = json.dumps(anon.get(READY_URL).json())
        assert str(row.id) not in raw
        assert SECRET_MARKER not in raw
        assert "t-1" not in raw
        assert "HTTP 500" not in raw
