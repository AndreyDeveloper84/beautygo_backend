"""DRF-2525 — ``replay_dead_outbox_events`` must not widen past the named topic.

Шесть мёртвых ``booking.completed`` салона formula-tela нужно переотправить.
В том же ящике у того же салона лежат мёртвые ``booking.created``: их
переотправка — это прокси в боте и сообщение клиенту «вы записаны» по брони
месячной давности. Необратимо и видно клиенту.

До этой правки у команды не было фильтра по теме: ``--tenant`` и ``--since``
поднимали ВСЕ мёртвые письма салона или окна. Узлы ниже держат две вещи:

* **отрицательная пара** — ``--tenant … --topic booking.completed`` оставляет
  мёртвый ``booking.created`` того же салона мёртвым;
* **положительная пара** — нужная тема при этом действительно доехала до
  ``pending`` (иначе «created не задет» проходило бы и у команды, которая не
  делает ничего);
* ``--tenant`` / ``--since`` без ``--topic`` — отказ ДО любой записи: забытый
  фильтр не должен молча расширять область.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from appointments.models import OutboxEvent

TENANT = "b32a057a-0000-0000-0000-000000000001"
OTHER_TENANT = "b32a057a-0000-0000-0000-000000000002"
DEAD = OutboxEvent.BotDeliveryStatus.DEAD
PENDING = OutboxEvent.BotDeliveryStatus.PENDING


def _dead(topic: str, tenant: str = TENANT, **kwargs) -> OutboxEvent:
    defaults = dict(
        topic=topic,
        payload={"event_id": "x", "tenant_id": tenant, "data": {}},
        external_delivery_enabled=True,
        bot_delivery_status=DEAD,
        bot_dead_lettered_at=timezone.now() - timedelta(days=20),
        bot_attempt_count=8,
        bot_last_error="HTTP 500",
    )
    defaults.update(kwargs)
    return OutboxEvent.objects.create(**defaults)


def _status(row: OutboxEvent) -> str:
    row.refresh_from_db()
    return row.bot_delivery_status


@pytest.mark.django_db
class TestTopicNarrowsTenant:
    def test_completed_replayed_created_of_same_tenant_untouched(self):
        completed = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)

        call_command(
            "replay_dead_outbox_events",
            "--tenant", TENANT, "--topic", "booking.completed",
            stdout=StringIO(),
        )

        # Положительная пара: нужная тема доехала.
        assert _status(completed) == PENDING
        # Отрицательная: created того же салона остался мёртвым.
        assert _status(created) == DEAD

    def test_topic_does_not_leak_to_other_tenant(self):
        mine = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        foreign = _dead(OutboxEvent.Topic.BOOKING_COMPLETED, tenant=OTHER_TENANT)

        call_command(
            "replay_dead_outbox_events",
            "--tenant", TENANT, "--topic", "booking.completed",
            stdout=StringIO(),
        )

        assert _status(mine) == PENDING
        assert _status(foreign) == DEAD

    def test_topic_narrows_since(self):
        completed = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)
        cutoff = (timezone.now() - timedelta(days=30)).isoformat()

        call_command(
            "replay_dead_outbox_events",
            "--since", cutoff, "--topic", "booking.completed",
            stdout=StringIO(),
        )

        assert _status(completed) == PENDING
        assert _status(created) == DEAD

    def test_topic_repeatable(self):
        completed = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        cancelled = _dead(OutboxEvent.Topic.BOOKING_CANCELLED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)

        call_command(
            "replay_dead_outbox_events",
            "--tenant", TENANT,
            "--topic", "booking.completed", "--topic", "booking.cancelled",
            stdout=StringIO(),
        )

        assert _status(completed) == PENDING
        assert _status(cancelled) == PENDING
        assert _status(created) == DEAD

    def test_dry_run_with_topic_counts_only_that_topic(self):
        _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)
        out = StringIO()

        call_command(
            "replay_dead_outbox_events",
            "--tenant", TENANT, "--topic", "booking.completed", "--dry-run",
            stdout=out,
        )

        assert "Matched dead-lettered rows: 1" in out.getvalue()
        assert "topic=booking.created" not in out.getvalue()
        assert _status(created) == DEAD


@pytest.mark.django_db
class TestWideningWithoutTopicRefused:
    @pytest.mark.parametrize(
        "selector",
        [
            ("--tenant", TENANT),
            ("--since", (timezone.now() - timedelta(days=30)).isoformat()),
        ],
        ids=["tenant", "since"],
    )
    def test_refused_before_any_write(self, selector):
        completed = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)

        with pytest.raises(CommandError, match="--topic"):
            call_command("replay_dead_outbox_events", *selector, stdout=StringIO())

        assert _status(completed) == DEAD
        assert _status(created) == DEAD

    def test_refused_even_on_dry_run(self):
        # Отказ — свойство области, а не режима: сухой прогон без темы показал
        # бы оператору число, которое потом не совпадёт с запуском.
        with pytest.raises(CommandError, match="--topic"):
            call_command(
                "replay_dead_outbox_events", "--tenant", TENANT, "--dry-run",
                stdout=StringIO(),
            )

    def test_event_id_needs_no_topic(self):
        # Точечный выбор по id уже адресен — тема ему не нужна.
        target = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)

        call_command(
            "replay_dead_outbox_events", "--event-id", str(target.id),
            stdout=StringIO(),
        )

        assert _status(target) == PENDING
        assert _status(created) == DEAD

    def test_topic_alone_is_a_selector(self):
        completed = _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        created = _dead(OutboxEvent.Topic.BOOKING_CREATED)

        call_command(
            "replay_dead_outbox_events", "--topic", "booking.completed",
            stdout=StringIO(),
        )

        assert _status(completed) == PENDING
        assert _status(created) == DEAD

    def test_unknown_topic_refused(self):
        _dead(OutboxEvent.Topic.BOOKING_COMPLETED)
        # «invalid choice», а не «unrecognized arguments»: опечатка в теме должна
        # падать на словаре тем, а не на отсутствии флага.
        with pytest.raises(CommandError, match="invalid choice"):
            call_command(
                "replay_dead_outbox_events",
                "--tenant", TENANT, "--topic", "booking.complete",
                stdout=StringIO(),
            )

    def test_all_with_topic_refused(self):
        with pytest.raises(CommandError, match="--all cannot be combined"):
            call_command(
                "replay_dead_outbox_events", "--all", "--topic", "booking.completed",
                stdout=StringIO(),
            )
