"""Tests for the outbox dispatcher Celery task.

All tests run with ``CELERY_TASK_ALWAYS_EAGER=True`` (set in
``settings/test.py``) so calling the task is synchronous — no broker,
no worker. Real-Redis end-to-end coverage is opt-in via the
``@pytest.mark.integration`` marker.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from appointments.models import OutboxEvent
from appointments.tasks import (
    EVENT_HANDLERS,
    LAG_ALERT_THRESHOLD,
    dispatch_outbox_events,
)


@pytest.mark.django_db
class TestDispatchOutboxEvents:

    def test_processes_pending_events(self):
        """Pending events get a processed_at stamp and the handler runs."""
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"appointment_id": "abc-123"},
        )
        result = dispatch_outbox_events()

        assert result == {"processed": 1, "failed": 0, "skipped": 0}
        event.refresh_from_db()
        assert event.processed_at is not None
        assert event.error_count == 0

    def test_skips_already_processed_events(self):
        """Events with processed_at set must not be touched on a subsequent tick."""
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"appointment_id": "abc-456"},
            processed_at=timezone.now(),
        )
        original = event.processed_at

        result = dispatch_outbox_events()

        assert result["processed"] == 0
        event.refresh_from_db()
        assert event.processed_at == original

    def test_unknown_topic_marked_failed(self):
        """Topic with no registered handler increments error_count + last_error.
        Avoids blowing up the whole batch on schema drift between deploys."""
        event = OutboxEvent.objects.create(
            topic="nonexistent.topic",
            payload={},
        )
        result = dispatch_outbox_events()

        assert result == {"processed": 0, "failed": 1, "skipped": 0}
        event.refresh_from_db()
        assert event.processed_at is None
        assert event.error_count == 1
        assert "no handler registered" in event.last_error

    def test_handler_exception_recorded(self):
        """Handler raise → error counted + saved + remains pending for retry."""
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={},
        )

        def boom(_):
            raise ValueError("downstream timeout")

        with patch.dict(
            EVENT_HANDLERS,
            {OutboxEvent.Topic.BOOKING_CREATED: boom},
            clear=False,
        ):
            result = dispatch_outbox_events()

        assert result == {"processed": 0, "failed": 1, "skipped": 0}
        event.refresh_from_db()
        assert event.processed_at is None
        assert event.error_count == 1
        assert "ValueError" in event.last_error
        assert "downstream timeout" in event.last_error

    def test_event_dead_lettered_after_max_attempts(self):
        """error_count >= MAX_HANDLER_ATTEMPTS => skipped, never re-tried."""
        from appointments.tasks import MAX_HANDLER_ATTEMPTS

        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={},
            error_count=MAX_HANDLER_ATTEMPTS,
            last_error="prior failures",
        )
        result = dispatch_outbox_events()

        assert result == {"processed": 0, "failed": 0, "skipped": 1}
        event.refresh_from_db()
        # Untouched: still pending, error_count unchanged.
        assert event.processed_at is None
        assert event.error_count == MAX_HANDLER_ATTEMPTS

    def test_batch_processes_multiple_events_in_order(self):
        """Older events go first; oldest's processed_at <= newer's."""
        first = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"order": 1},
        )
        second = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CONFIRMED,
            payload={"order": 2},
        )

        dispatch_outbox_events()

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.processed_at is not None
        assert second.processed_at is not None
        assert first.processed_at <= second.processed_at


@pytest.mark.django_db
class TestLagAlert:
    """The dispatcher logs ERROR when the oldest pending row is older than
    LAG_ALERT_THRESHOLD. Sentry captures the ERROR and pages on-call.

    Patching the module logger directly (instead of pytest's caplog) is
    deliberate: settings/base.py LOGGING sets ``propagate=False`` on the
    ``appointments`` logger so it can route to its own handler chain,
    which means caplog's root-attached handler never receives records.
    Spying on the logger object is the supported way to assert against
    this code path.
    """

    def test_no_alert_when_queue_empty(self):
        with patch("appointments.tasks.logger") as mock_logger:
            dispatch_outbox_events()
        # Empty queue → no lag-check log. (info / debug calls may fire
        # in other branches; assert specifically no error.)
        assert not mock_logger.error.called

    def test_no_alert_when_pending_row_is_young(self):
        OutboxEvent.objects.create(
            topic="nonexistent.no_handler",  # stays pending — failed
            payload={},
        )
        with patch("appointments.tasks.logger") as mock_logger:
            dispatch_outbox_events()
        # No 'outbox.lag_breach' error — the row is fresh.
        lag_calls = [
            c for c in mock_logger.error.call_args_list
            if c.args and "outbox.lag_breach" in c.args[0]
        ]
        assert lag_calls == []

    def test_alert_fires_for_old_pending_row(self):
        # Create a row, then back-date created_at past the threshold.
        # The lag SLO is about oldest unprocessed — pre-date the row to
        # simulate a worker that hasn't drained the queue for >5 min.
        event = OutboxEvent.objects.create(
            topic="nonexistent.no_handler",
            payload={},
        )
        OutboxEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - LAG_ALERT_THRESHOLD - timedelta(seconds=30),
        )
        with patch("appointments.tasks.logger") as mock_logger:
            dispatch_outbox_events()
        lag_calls = [
            c for c in mock_logger.error.call_args_list
            if c.args and "outbox.lag_breach" in c.args[0]
        ]
        assert len(lag_calls) == 1, (
            f"expected exactly one outbox.lag_breach ERROR, "
            f"got error calls: {mock_logger.error.call_args_list}"
        )


@pytest.mark.django_db
class TestHandlerRegistry:
    """Make sure every Topic in the model has a registered handler.
    A new topic added without a handler is a deploy footgun — this test
    catches it at PR review."""

    def test_every_known_topic_has_handler(self):
        for topic, _label in OutboxEvent.Topic.choices:
            assert topic in EVENT_HANDLERS, (
                f"Topic '{topic}' is declared on OutboxEvent but has no "
                f"entry in EVENT_HANDLERS. Add a handler in "
                f"appointments/tasks.py."
            )

    def test_booking_completed_chains_billing_before_notifications(self):
        """P5 (W2 R-5): booking.completed must run billing's fee accrual
        FIRST and the notifications handler SECOND — a plain
        EVENT_HANDLERS.update from billing would silently drop the
        notification leg. Closure inspection: the chain is built once
        at import time."""
        from billing.handlers import on_booking_completed
        from notifications.outbox_handlers import handle_booking_completed

        handler = EVENT_HANDLERS[OutboxEvent.Topic.BOOKING_COMPLETED]
        legs = [c.cell_contents for c in (handler.__closure__ or ())]
        assert legs[0] is on_booking_completed
        assert legs[1] is handle_booking_completed

    def test_chain_handlers_runs_legs_in_order(self):
        """The composer itself: first leg runs before the second."""
        from appointments.tasks import _chain_handlers
        calls = []
        _chain_handlers(
            lambda e: calls.append("billing"),
            lambda e: calls.append("notifications"),
        )(object())
        assert calls == ["billing", "notifications"]


@pytest.mark.django_db
class TestExhaustedRowsDoNotHoldBackIndependentEvents:
    """DRF-2773: «неподдерживаемое событие не должно бесконечно задерживать
    независимые события» (решение владельца 05.10 по DRF-2434).

    Исчерпанная строка навсегда остаётся с ``processed_at IS NULL``. Пока она
    выбиралась в батч и только пропускалась, сто таких строк занимали весь
    батч каждый тик. Узлы — тот же замер, которым дефект найден: полный батч
    исчерпанных, на одну меньше (положительный контроль) и неизвестный топик.
    """

    #: Обработчик только пишет в лог и полезной нагрузки не требует.
    HEALTHY = OutboxEvent.Topic.SYSTEM_MODULE_HEALTH_DEGRADED

    def _exhausted(self, count: int) -> None:
        from appointments.tasks import MAX_HANDLER_ATTEMPTS

        ids = [
            OutboxEvent.objects.create(
                topic="probe.unknown", payload={}, error_count=MAX_HANDLER_ATTEMPTS,
            ).id
            for _ in range(count)
        ]
        # Старше здоровой — иначе по порядку ``created_at`` они бы и так шли после неё.
        OutboxEvent.objects.filter(id__in=ids).update(created_at=timezone.now() - timedelta(hours=1))

    def test_a_full_batch_of_exhausted_rows_does_not_hold_back_a_healthy_event(self):
        from appointments.tasks import BATCH_SIZE

        self._exhausted(BATCH_SIZE)
        healthy = OutboxEvent.objects.create(topic=self.HEALTHY, payload={"module": "probe"})

        result = dispatch_outbox_events()

        healthy.refresh_from_db()
        assert healthy.processed_at is not None
        assert result == {"processed": 1, "failed": 0, "skipped": BATCH_SIZE}

    def test_one_slot_short_the_healthy_event_was_already_processed(self):
        """Положительный контроль: и до правки здоровая проходила, если в батче было место."""
        from appointments.tasks import BATCH_SIZE

        self._exhausted(BATCH_SIZE - 1)
        healthy = OutboxEvent.objects.create(topic=self.HEALTHY, payload={"module": "probe"})

        result = dispatch_outbox_events()

        healthy.refresh_from_db()
        assert healthy.processed_at is not None
        assert result["processed"] == 1

    def test_exhausted_rows_are_still_never_retried(self):
        from appointments.tasks import BATCH_SIZE, MAX_HANDLER_ATTEMPTS

        self._exhausted(BATCH_SIZE)
        dispatch_outbox_events()

        assert not OutboxEvent.objects.filter(topic="probe.unknown", processed_at__isnull=False).exists()
        assert set(
            OutboxEvent.objects.filter(topic="probe.unknown").values_list("error_count", flat=True)
        ) == {MAX_HANDLER_ATTEMPTS}

    def test_an_unknown_topic_becomes_exhausted_and_then_leaves_the_batch(self):
        from appointments.tasks import MAX_HANDLER_ATTEMPTS

        unknown = OutboxEvent.objects.create(topic="probe.unknown", payload={})
        results = [dispatch_outbox_events() for _ in range(MAX_HANDLER_ATTEMPTS + 1)]

        unknown.refresh_from_db()
        assert [r["failed"] for r in results] == [1] * MAX_HANDLER_ATTEMPTS + [0]
        assert results[-1]["skipped"] == 1
        assert (unknown.processed_at, unknown.error_count) == (None, MAX_HANDLER_ATTEMPTS)
