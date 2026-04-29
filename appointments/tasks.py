"""Celery tasks for appointments — primarily the outbox dispatcher.

The transactional-outbox pattern guarantees events get delivered even if
the worker crashes between handler call and ack. Domain code writes
``OutboxEvent`` rows in the same DB transaction as the change that
emitted them; this dispatcher is the single read-and-process loop.

Today every handler is a logging stub — the notifications app, push
infra, and SMS pipeline land in later PRs. Those apps will register
real handlers in ``EVENT_HANDLERS`` without touching this file.
"""
from __future__ import annotations

import logging
from typing import Callable

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import OutboxEvent

logger = logging.getLogger(__name__)


# Per-event-type handler registry. Add handlers here when the matching
# downstream app lands; the dispatcher routes by ``OutboxEvent.topic``.
EventHandler = Callable[[OutboxEvent], None]


def _log_handler(name: str) -> EventHandler:
    """Return a log-only handler for stubbing topics until the real app
    lands. Logs at INFO so deploys see traffic on the topic."""
    def handler(event: OutboxEvent) -> None:
        logger.info(
            "outbox.stub_handler topic=%s event_id=%s payload=%s",
            name, event.id, event.payload,
        )
    return handler


EVENT_HANDLERS: dict[str, EventHandler] = {
    OutboxEvent.Topic.BOOKING_CREATED: _log_handler("booking.created"),
    OutboxEvent.Topic.BOOKING_CONFIRMED: _log_handler("booking.confirmed"),
    OutboxEvent.Topic.BOOKING_CANCELLED: _log_handler("booking.cancelled"),
    OutboxEvent.Topic.BOOKING_RESCHEDULED: _log_handler("booking.rescheduled"),
    OutboxEvent.Topic.BOOKING_COMPLETED: _log_handler("booking.completed"),
    OutboxEvent.Topic.BOOKING_NO_SHOW: _log_handler("booking.no_show"),
    OutboxEvent.Topic.PAYMENT_CONFIRMED: _log_handler("payment.confirmed"),
    OutboxEvent.Topic.PAYMENT_REFUNDED: _log_handler("payment.refunded"),
    OutboxEvent.Topic.CACHE_INVALIDATE_SLOTS: _log_handler("cache.invalidate_slots"),
}


# Bind real notification handlers from the notifications app over the stub
# log handlers above. Two-stage init lets us keep _log_handler as the
# documented fallback for any topic that hasn't been wired yet — adding a
# new app that responds to outbox events is just an `EVENT_HANDLERS.update`
# from that app's module.
def _register_notification_handlers() -> None:
    try:
        from notifications.outbox_handlers import BOOKING_HANDLERS
    except ImportError:  # pragma: no cover — notifications app missing
        logger.exception("outbox.notifications_handlers_import_failed")
        return
    EVENT_HANDLERS.update(BOOKING_HANDLERS)


_register_notification_handlers()


# Max times a single event will go through the dispatcher before we stop
# touching it. After this it stays in DB with a non-empty ``last_error`` —
# ops can replay manually via a future management command. The bookkeeping
# here is per-row (``error_count``), not Celery's task retry — Celery
# retries the whole batch task, this counter survives across runs.
MAX_HANDLER_ATTEMPTS = 5

# Batch size — outbox queue is usually empty, this just bounds worst-case
# work on a tick. select_for_update with skip_locked ensures two workers
# can't double-handle the same row.
BATCH_SIZE = 100


@shared_task(name="appointments.tasks.dispatch_outbox_events")
def dispatch_outbox_events() -> dict:
    """Process pending OutboxEvent rows.

    Run periodically (10s by default — see ``CELERY_BEAT_SCHEDULE`` in
    settings/base.py). Always atomic-per-event: a successful handler
    call commits ``processed_at``; a failing one commits an incremented
    ``error_count``. Crashes between the two leave the row claimed but
    not processed — next tick the row reappears (lock released on
    transaction abort).

    Returns a small status dict so Celery's result backend has something
    useful for monitoring / logs.
    """
    processed = 0
    failed = 0
    skipped = 0

    with transaction.atomic():
        # Lock the batch; ``skip_locked`` makes parallel workers cooperate
        # rather than block on each other.
        rows = list(
            OutboxEvent.objects
            .select_for_update(skip_locked=True)
            .filter(processed_at__isnull=True)
            .order_by('created_at')[:BATCH_SIZE]
        )

        for event in rows:
            if event.error_count >= MAX_HANDLER_ATTEMPTS:
                skipped += 1
                continue

            handler = EVENT_HANDLERS.get(event.topic)
            if handler is None:
                # Unknown topic — store an error and skip rather than blow
                # up the whole batch. New topics need a registry entry.
                event.error_count += 1
                event.last_error = f"no handler registered for topic '{event.topic}'"
                event.save(update_fields=["error_count", "last_error"])
                failed += 1
                logger.warning(
                    "outbox.unknown_topic topic=%s event_id=%s",
                    event.topic, event.id,
                )
                continue

            try:
                handler(event)
            except Exception as exc:  # pragma: no cover — handlers are stubs today
                event.error_count += 1
                event.last_error = f"{exc.__class__.__name__}: {exc}"
                event.save(update_fields=["error_count", "last_error"])
                failed += 1
                logger.exception(
                    "outbox.handler_failed topic=%s event_id=%s",
                    event.topic, event.id,
                )
                continue

            event.processed_at = timezone.now()
            event.save(update_fields=["processed_at"])
            processed += 1

    if processed or failed or skipped:
        logger.info(
            "outbox.dispatch_summary processed=%d failed=%d skipped=%d",
            processed, failed, skipped,
        )

    return {"processed": processed, "failed": failed, "skipped": skipped}
