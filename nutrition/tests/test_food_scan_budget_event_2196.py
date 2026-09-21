"""Сигнал бюджета распознавания уходит боту событием (DRF-2196, вариант а1).

До этого листа ``_signal_if_crossed`` знал про 80 / 100 % общего потолка,
дедуплицировал по суткам, складывал стоимость — и отправлял сигнал ровно
туда, где его никто не прочтёт: ``sentry_sdk.capture_message``, а
``SENTRY_DSN`` на пилоте пуст. Работающий канал к операторам — MAX — живёт
в боте (``alerting.page``, DRF-2158), и у бота уже есть ядро, которое
решает, звучать ли странице (``apps/observability/scan_budget_alert``).

Владелец выбрал вариант (а1), §64: каталог публикует
``system.module.health.degraded`` через outbox (HMAC, ретраи, dead-letter,
ручной реплей — рельс уже есть), бот передаёт в MAX.

# Что здесь пришпилено

* событие уходит на пересечении порога — тем же дедупом по суткам, что и
  прежний сигнал, не отдельным;
* конверт **без пользователя и без тенанта**: это системный сигнал,
  каталог считает снимки, а не тех, кто их прислал. Контракт бота
  ослаблен ровно для ``system.*`` по слову владельца (§64);
* в данных нет ничего, чем человека можно назвать;
* отказ публикации не роняет скан — как и отказ Sentry до этого;
* строка идёт боту только когда тема названа в
  ``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` — флажок держит владелец, пока
  потребитель бота не зелёный «в оба конца».
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import DatabaseError

from appointments.infrastructure.outbox.envelope import EVENT_VERSIONS
from appointments.models import OutboxEvent
from nutrition.services import food_scan_budget as budget
# Стенд переиспользован: `_settings` — autouse (токен, потолки, пустой кэш),
# `now` — фикстура суток 2026-09-21 UTC. Фикстуры импортируются по имени, и
# flake8 видит их «неиспользуемыми» — pytest находит их именно так.
from nutrition.tests.test_food_scan_budget_2145 import (  # noqa: F401
    _post_scan,
    _router,
    _settings,
    now,
)

pytestmark = pytest.mark.django_db

TOPIC = "system.module.health.degraded"


def _events() -> list[OutboxEvent]:
    return list(OutboxEvent.objects.filter(topic=TOPIC).order_by("created_at"))


@pytest.fixture()
def five_total(now, settings):  # noqa: F811 — фикстура по имени, см. импорт
    """Потолок 5 на всех — 80 % на четвёртом, 100 % на пятом снимке."""
    settings.FOOD_SCAN_DAILY_PER_USER = 100
    settings.FOOD_SCAN_DAILY_TOTAL = 5
    return settings


class TestTheTopicIsRegistered:
    def test_the_topic_is_a_declared_outbox_topic(self) -> None:
        """`emit_outbox_event` отвергает незаявленную тему — заявить обязательно."""
        topics = {value for value, _label in OutboxEvent.Topic.choices}
        assert TOPIC in topics

    def test_the_topic_has_a_version(self) -> None:
        """Каждое событие между сервисами несёт `event_version` (ADR-0009, правило 7)."""
        assert EVENT_VERSIONS[TOPIC] == 1


class TestTheEventLeavesOnTheCrossing:
    def test_80_percent_emits_one_warning_event(self, five_total) -> None:
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        events = _events()
        assert len(events) == 1
        data = events[0].payload["data"]
        assert data["module_name"] == "nutrition.food_scan"
        assert data["severity"] == "warning"
        assert data["metric"]["used"] == 4
        assert data["metric"]["limit"] == 5
        # Половина полезной нагрузки для оператора — сутки и стоимость.
        assert data["metric"]["day"] == "2026-09-21"
        # Цены не заданы → «не посчитано» = JSON null, не строка "n/a":
        # ядро бота говорит оператору «не посчитана», а "n/a" вывело бы как есть.
        assert data["metric"]["cost_usd"] is None

    def test_100_percent_emits_an_error_event(self, five_total) -> None:
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(5):
                _post_scan(router, f"bot:{i}")

        severities = [e.payload["data"]["severity"] for e in _events()]
        assert severities == ["warning", "error"]

    def test_the_same_day_does_not_emit_twice(self, five_total) -> None:
        """Тем же дедупом по суткам, что и прежний сигнал, — не своим."""
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(5):
                _post_scan(router, f"bot:{i}")
            _post_scan(router, "bot:extra")  # 503, 100 % второй раз
            _post_scan(router, "bot:extra2")

        assert len(_events()) == 2

    def test_below_80_emits_nothing(self, five_total) -> None:
        """Положительная пара к «на 80 % уходит»: ниже порога — ничего."""
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(3):
                _post_scan(router, f"bot:{i}")

        assert _events() == []


class TestTheEnvelopeHasNoPerson:
    def test_no_user_and_no_tenant(self, five_total) -> None:
        """Системный сигнал: каталог считает снимки, а не тех, кто их прислал."""
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        envelope = _events()[0].payload
        # Наличие — первым: конверт полный, не пустышка.
        assert envelope["event_name"] == TOPIC
        assert envelope["event_version"] == 1
        assert envelope["actor"] == "system"
        assert envelope["user_id"] is None
        assert envelope["tenant_id"] is None

    def test_the_data_names_no_one(self, five_total) -> None:
        """Точные наборы ключей: любое лишнее поле — красное.

        Проверка подстроками («нет `bot:`») не срабатывала бы никогда:
        `used` и `limit` внешнего id не содержат, а новое поле с человеком
        прошло бы мимо. Точное равенство ключей ловит именно это.
        """
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        data = _events()[0].payload["data"]
        assert set(data) == {"module_name", "severity", "metric"}
        assert set(data["metric"]) == {"used", "limit", "day", "cost_usd"}


class TestTheEventNeverBreaksTheScan:
    def test_a_failing_emit_does_not_break_the_scan(self, five_total) -> None:
        """Настоящий путь: ошибка БД из `emit_outbox_event`, а не подменённая обёртка."""
        router = _router()
        with (
            patch.object(budget, "_send_signal", return_value=True) as sentry,
            patch(
                "appointments.infrastructure.outbox.envelope.emit_outbox_event",
                side_effect=DatabaseError("insert failed"),
            ),
        ):
            responses = [_post_scan(router, f"bot:{i}") for i in range(4)]

        assert all(r.status_code == 200 for r in responses)
        # И прежний сигнал своё отработал — отказ одного канала не глушит другой.
        assert sentry.call_count == 1

    def test_a_failing_sentry_does_not_stop_the_event(self, five_total) -> None:
        """Обратное направление: Sentry упал — событие всё равно ушло.

        Без этого узла эмит можно было бы спрятать внутрь `try` Sentry, и
        первый же сбой Sentry глушил бы единственный живой канал.
        """
        router = _router()
        with patch.object(budget, "_send_signal", side_effect=RuntimeError("sentry down")):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        assert len(_events()) == 1

    def test_a_failed_emit_is_retried_on_the_next_crossing_scan(self, five_total) -> None:
        """Сбой публикации возвращает счёт суток — следующий скан переспросит.

        Ключ прежнего сигнала занят ДО публикации, поэтому без своего ключа
        и его возврата один сбой INSERT крал бы единственное оповещение за
        сутки.
        """
        router = _router()
        calls = {"n": 0}
        real = __import__(
            "appointments.infrastructure.outbox.envelope", fromlist=["emit_outbox_event"]
        ).emit_outbox_event

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise DatabaseError("insert failed once")
            return real(**kwargs)

        with (
            patch.object(budget, "_send_signal", return_value=True),
            patch("appointments.infrastructure.outbox.envelope.emit_outbox_event", flaky),
        ):
            for i in range(4):
                _post_scan(router, f"bot:{i}")  # 80 % — первая попытка падает
            assert _events() == []
            budget._emit_budget_event_once(
                level="warning", used=4, limit=5, day="2026-09-21", ttl=3600
            )

        assert len(_events()) == 1

    def test_positive_pair_a_delivered_event_is_not_repeated(self, five_total) -> None:
        """Иначе возврат счёта выродился бы в «дедупа нет»."""
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")
            budget._emit_budget_event_once(
                level="warning", used=4, limit=5, day="2026-09-21", ttl=3600
            )

        assert len(_events()) == 1


class TestTheOwnerHoldsTheFlip:
    def test_the_row_stays_local_until_the_topic_is_listed(self, five_total) -> None:
        five_total.OUTBOX_EXTERNAL_DELIVERY_TOPICS = ()
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        assert _events()[0].external_delivery_enabled is False

    def test_positive_pair_listed_topic_ships(self, five_total) -> None:
        five_total.OUTBOX_EXTERNAL_DELIVERY_TOPICS = (TOPIC,)
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True):
            for i in range(4):
                _post_scan(router, f"bot:{i}")

        assert _events()[0].external_delivery_enabled is True
