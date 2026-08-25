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
from datetime import datetime, timedelta
from typing import Callable, NamedTuple

from celery import shared_task
from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from .domain.value_objects import OperationalActor
from .infrastructure.outbox.publisher import (
    publish_outbox_events_to_bot as _publish_to_bot,
)
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
    # Wave 1 canonical topic — log-only in-process stub. Real push
    # notification + cache invalidation stay wired to the legacy
    # BOOKING_RESCHEDULED topic below (via _register_notification_handlers)
    # so they fire exactly once per reschedule; this topic exists purely
    # so cross-service consumption can be turned on later (Block C
    # publisher gate, OUTBOX_EXTERNAL_DELIVERY_TOPICS) without the
    # dispatcher dead-lettering it in the meantime.
    OutboxEvent.Topic.APPOINTMENT_RESCHEDULED: _log_handler("appointment.rescheduled"),
    OutboxEvent.Topic.BOOKING_COMPLETED: _log_handler("booking.completed"),
    OutboxEvent.Topic.BOOKING_NO_SHOW: _log_handler("booking.no_show"),
    # B-1a — renamed payment.confirmed → payment.captured (Variant C).
    OutboxEvent.Topic.PAYMENT_CAPTURED: _log_handler("payment.captured"),
    # B-1b — new payment.failed topic emitted on YooKassa fail/canceled.
    OutboxEvent.Topic.PAYMENT_FAILED: _log_handler("payment.failed"),
    OutboxEvent.Topic.PAYMENT_REFUNDED: _log_handler("payment.refunded"),
    OutboxEvent.Topic.CACHE_INVALIDATE_SLOTS: _log_handler("cache.invalidate_slots"),
    # #246 Q1 — log-only stub. Real cross-service consumer is bot-
    # platform (per ADR-0009 §Domain ownership matrix). The Ayla side
    # only persists + logs; consumers dedupe by event_id on their end.
    OutboxEvent.Topic.TENANT_RELATIONSHIP_REVOKED: _log_handler(
        "tenant.relationship.revoked",
    ),
    # W2 billing producer topics (C4, P4) — cross-service consumers only
    # (W3 bot notifications + analytics); the Ayla side persists + logs,
    # same rationale as the TUR stub above.
    OutboxEvent.Topic.SUBSCRIPTION_ACTIVATED: _log_handler(
        "subscription.activated",
    ),
    OutboxEvent.Topic.SUBSCRIPTION_PAST_DUE: _log_handler(
        "subscription.past_due",
    ),
    OutboxEvent.Topic.BILLING_FEE_CHARGED: _log_handler(
        "billing.fee_charged",
    ),
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


def _chain_handlers(first: EventHandler, second: EventHandler) -> EventHandler:
    """Compose two handlers into one — order matters."""
    def chained(event: OutboxEvent) -> None:
        first(event)
        second(event)
    return chained


def _register_billing_handlers() -> None:
    """W2 R-5 / P5: booking.completed fee accrual CHAINED with the
    notifications handler — NEVER a replacement (a plain
    ``EVENT_HANDLERS.update`` from billing would silently drop the
    notification leg). Billing runs FIRST: money before pushes.
    billing.handlers.on_booking_completed swallows its own exceptions,
    so the notification leg always runs; if that leg fails, the
    dispatcher retries the whole chain and BookingFee's
    UNIQUE(appointment_id) keeps the billing re-run idempotent
    (AYLA-DEC-0010 / C4).
    """
    try:
        from billing.handlers import on_booking_completed
    except ImportError:  # pragma: no cover — billing app missing
        logger.exception("outbox.billing_handlers_import_failed")
        return
    topic = OutboxEvent.Topic.BOOKING_COMPLETED
    EVENT_HANDLERS[topic] = _chain_handlers(
        on_booking_completed, EVENT_HANDLERS[topic],
    )


_register_billing_handlers()


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

# Lag SLO — surface ERROR-level when the oldest unprocessed row is older
# than this. Sentry / structured logs capture the ERROR and on-call gets
# paged. 5 min matches ADR-0009 §Event contract guidance ("on-call alert
# on >5 min lag"). Not a Celery retry trigger — purely observability.
LAG_ALERT_THRESHOLD = timedelta(minutes=5)


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

    # Lag SLO check — runs every tick regardless of whether we processed
    # anything. A backlog the dispatcher can't drain (handler stuck, broker
    # off, dead-lettered batch) shows up as the oldest pending row aging
    # past LAG_ALERT_THRESHOLD.
    oldest_pending = OutboxEvent.objects.filter(
        processed_at__isnull=True,
    ).aggregate(oldest=Min("created_at"))["oldest"]
    if oldest_pending is not None:
        lag = timezone.now() - oldest_pending
        if lag > LAG_ALERT_THRESHOLD:
            logger.error(
                "outbox.lag_breach lag_seconds=%d threshold_seconds=%d",
                int(lag.total_seconds()),
                int(LAG_ALERT_THRESHOLD.total_seconds()),
            )

    return {"processed": processed, "failed": failed, "skipped": skipped}


@shared_task(name="appointments.tasks.publish_outbox_events_to_bot")
def publish_outbox_events_to_bot() -> dict:
    """Block C → C2 — cross-service HTTP publisher beat task.

    Thin Celery wrapper around
    :func:`appointments.infrastructure.outbox.publisher.publish_outbox_events_to_bot`.
    The implementation lives in the infrastructure module so it can be
    invoked directly from tests without the Celery harness. This task
    just translates the dataclass summary into the dict shape Celery's
    result backend prefers.

    Scheduled every 30 seconds (see ``CELERY_BEAT_SCHEDULE``). Outcome
    counts land in the result backend for monitoring; the publisher
    also logs warnings on dead-lettered rows so on-call can act on
    them via the C5 replay management command.
    """
    summary = _publish_to_bot()
    return {
        "sent": summary.sent,
        "failed": summary.failed,
        "dead": summary.dead,
        "scanned": summary.scanned,
    }


class SweepWindow(NamedTuple):
    """What the sweep is allowed to touch on this tick — or why it isn't.

    ``refusal`` is a short, stable token (never a sentence) because it
    goes into the pass line as ``reason=<token>`` and operators grep for
    it. Empty means the sweep may run.
    """
    not_before: datetime | None = None
    cutoff: datetime | None = None
    refusal: str = ""

    @property
    def may_run(self) -> bool:
        return not self.refusal


def _auto_complete_window() -> SweepWindow:
    """Resolve the window for the sweep, or the reason it must not run.

    Fail closed: see ``BOOKING_AUTO_COMPLETE_ENABLED`` in settings for
    why a silent full-backlog sweep is the outcome worth refusing.

    The refusal used to be a bare ``None`` and — for the gated-off case —
    a completely silent one. That is what made DRF-1048 take weeks to
    read: a beat entry firing every 15 minutes and writing nothing looks
    exactly like a beat entry that never fires. The reason now travels
    back to the caller, which puts it on the one line every pass emits.
    """
    from django.conf import settings
    from django.utils.dateparse import parse_datetime

    if not getattr(settings, "BOOKING_AUTO_COMPLETE_ENABLED", False):
        return SweepWindow(refusal="disabled")

    raw_floor = getattr(settings, "BOOKING_AUTO_COMPLETE_NOT_BEFORE", "") or ""
    not_before = parse_datetime(raw_floor) if raw_floor else None
    if not_before is None or timezone.is_naive(not_before):
        logger.error(
            "booking.auto_complete.misconfigured — "
            "BOOKING_AUTO_COMPLETE_ENABLED is on but "
            "BOOKING_AUTO_COMPLETE_NOT_BEFORE is %s. Refusing to sweep: "
            "without a floor this would complete (and bill, and request "
            "reviews for) every elapsed booking in history. Set it to the "
            "moment the feature goes live, and drain anything older with "
            "manage.py complete_elapsed_backlog.",
            "empty" if not raw_floor else f"unusable ({raw_floor!r})",
        )
        return SweepWindow(refusal="no_floor")

    hours = getattr(settings, "BOOKING_AUTO_COMPLETE_AFTER_HOURS", 3)
    cutoff = timezone.now() - timedelta(hours=hours)
    return SweepWindow(not_before=not_before, cutoff=cutoff)


def _empty_pass_diagnosis(*, not_before, cutoff) -> dict:
    """Why a pass that swept the window came back with nothing.

    An empty pass has two very different meanings and the count alone
    cannot separate them: nothing had elapsed, or plenty had and none of
    it was eligible. Two bounded counters answer the follow-up question
    without anyone opening a shell against the pilot database:

    ``elapsed_unconfirmed``
        elapsed inside the window but never reached CONFIRMED — the
        booking is stuck earlier in the lifecycle (unpaid hold, never
        confirmed). Not this task's business to close; very much its
        business to make visible, because the sweep is where the absence
        shows up first.
    ``below_floor``
        CONFIRMED and elapsed, but older than the floor. The backlog
        ``manage.py complete_elapsed_backlog`` exists to drain — a
        standing number, not an anomaly, but one nobody can see today.

    Only computed on an empty pass: on a productive tick the counts add
    nothing and the query is pure overhead.
    """
    from .models import Appointment

    return Appointment.objects.filter(end_datetime__lte=cutoff).aggregate(
        elapsed_unconfirmed=Count(
            "id",
            filter=Q(
                end_datetime__gte=not_before,
                status__in=(
                    Appointment.Status.PENDING,
                    Appointment.Status.AWAITING_PAYMENT,
                ),
            ),
        ),
        below_floor=Count(
            "id",
            filter=Q(
                end_datetime__lt=not_before,
                status=Appointment.Status.CONFIRMED,
            ),
        ),
    )


def complete_elapsed_bookings(*, not_before, cutoff, batch_size: int) -> dict:
    """Close confirmed bookings that ended before ``cutoff``.

    Shared by the beat task and the backlog command so the two cannot
    diverge in what they consider eligible or in what they emit.

    Each booking is its own transaction. One row that cannot be closed
    (a racing cancellation, a payment hook blowing up) must not roll back
    the ones already done — and a batch-wide transaction would also hold
    every row lock until the last handler finished.

    Idempotency comes from the state machine, not from bookkeeping: the
    status is re-read under ``select_for_update`` and
    ``Appointment.complete()`` refuses anything that is no longer
    CONFIRMED. Two workers racing the same row therefore produce one
    transition and one event.
    """
    from appointments.application.services.completion import (
        close_booking, schedule_capture_safely,
    )
    from django.core.exceptions import ValidationError

    from .models import Appointment

    candidate_ids = list(
        Appointment.objects
        .filter(
            status=Appointment.Status.CONFIRMED,
            end_datetime__lte=cutoff,
            end_datetime__gte=not_before,
        )
        .order_by("end_datetime")
        .values_list("id", flat=True)[:batch_size]
    )

    completed = 0
    skipped = 0
    failed = 0
    for appointment_id in candidate_ids:
        try:
            with transaction.atomic():
                appointment = (
                    Appointment.objects
                    # of=('self',) — lock ONLY the base table; the
                    # nullable service FK would otherwise make Postgres
                    # reject the FOR UPDATE (same trap as the HTTP path).
                    .select_for_update(of=("self",))
                    .select_related("specialist", "client", "service")
                    .get(pk=appointment_id)
                )
                if appointment.status != Appointment.Status.CONFIRMED:
                    # Closed, cancelled or moved between the scan and the
                    # lock. Not an error — the sweep lost a benign race.
                    skipped += 1
                    continue
                close_booking(
                    appointment,
                    completed_by=OperationalActor.SYSTEM.value,
                )
        except ValidationError:
            skipped += 1
            continue
        except Exception:  # noqa: BLE001 — one bad row must not stop the batch
            failed += 1
            logger.exception(
                "booking.auto_complete.failed appointment_id=%s",
                appointment_id,
            )
            continue

        completed += 1
        logger.info(
            "booking.auto_completed appointment_id=%s end_datetime=%s",
            appointment.id, appointment.end_datetime.isoformat(),
        )
        # After the commit, for the same reason the HTTP path does it
        # after its atomic block — the booking is durably completed even
        # if the payment provider is unreachable.
        schedule_capture_safely(appointment)

    # ``candidates`` is what separates "the sweep found nothing" from "the
    # sweep found things and closed none of them" — the two failures the
    # completed/skipped/failed triple alone cannot tell apart.
    return {
        "candidates": len(candidate_ids),
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
    }


@shared_task(name="appointments.tasks.auto_complete_elapsed_bookings")
def auto_complete_elapsed_bookings() -> dict:
    """Close visits that happened and that nobody closed (DRF-1064, block B).

    Deliberately separate from the manual closure path: this task never
    decides that a *particular* visit went well, it only records that a
    confirmed booking whose time passed hours ago is not going to be
    marked by hand. The closure is attributed to ``system`` so a consumer
    can tell "the salon closed this" from "nobody did, so we did".

    Elapsed time is weak evidence — ``Ayla MVP Appointment Contract §5``
    says so outright ("elapsed time alone is not completion evidence"),
    and OQ-AC-3 leaves the evidence model open. That is exactly why the
    attribution field exists: the fact is recorded together with how
    strongly it is known, rather than being laundered into a closure that
    looks like a human one.
    """
    window = _auto_complete_window()
    if not window.may_run:
        # DRF-1048. This branch used to `return` in silence for the
        # gated-off case, which is the whole reason the ticket sat open:
        # a sweep that never runs and a sweep that runs and refuses look
        # identical in an empty log. The refusal is now as loud as the
        # work — INFO, one line, naming the switch that would change it.
        logger.info(
            "booking.auto_complete.pass ran=false reason=%s "
            "candidates=0 completed=0 skipped=0 failed=0 "
            "(gated by BOOKING_AUTO_COMPLETE_ENABLED + "
            "BOOKING_AUTO_COMPLETE_NOT_BEFORE)",
            window.refusal,
        )
        return {
            "candidates": 0, "completed": 0, "skipped": 0, "failed": 0,
            "ran": False, "reason": window.refusal,
        }

    from django.conf import settings
    not_before, cutoff = window.not_before, window.cutoff
    result = complete_elapsed_bookings(
        not_before=not_before,
        cutoff=cutoff,
        batch_size=getattr(
            settings, "BOOKING_AUTO_COMPLETE_BATCH_SIZE", 200,
        ),
    )

    # One line per pass, unconditionally — including the pass that did
    # nothing. Silence has to mean "the task did not run", and nothing
    # else, or the next person reading this log is back where DRF-1048
    # started. The window travels with the counts because a wrong window
    # (timezone, grace period, floor) produces a legitimately empty pass
    # that is indistinguishable from a correct one without it.
    diagnosis = (
        _empty_pass_diagnosis(not_before=not_before, cutoff=cutoff)
        if result["candidates"] == 0
        else {"elapsed_unconfirmed": None, "below_floor": None}
    )
    logger.info(
        "booking.auto_complete.pass ran=true reason=ok "
        "candidates=%d completed=%d skipped=%d failed=%d "
        "cutoff=%s not_before=%s elapsed_unconfirmed=%s below_floor=%s",
        result["candidates"], result["completed"], result["skipped"],
        result["failed"], cutoff.isoformat(), not_before.isoformat(),
        "n/a" if diagnosis["elapsed_unconfirmed"] is None
        else diagnosis["elapsed_unconfirmed"],
        "n/a" if diagnosis["below_floor"] is None
        else diagnosis["below_floor"],
    )
    if result["failed"]:
        # A row the sweep could not close is not a routine outcome: the
        # visit happened, nothing downstream of completion fired, and no
        # later tick will retry it any harder. Separate ERROR line so it
        # is alertable without parsing the INFO summary.
        logger.error(
            "booking.auto_complete.rows_failed failed=%d candidates=%d",
            result["failed"], result["candidates"],
        )
    return {**result, "ran": True, "reason": "ok"}


@shared_task(name="appointments.tasks.purge_expired_idempotency_keys")
def purge_expired_idempotency_keys() -> dict:
    """Delete IdempotencyKey rows past their expires_at (#512).

    Run periodically (daily — see settings.base CELERY_BEAT_SCHEDULE).
    The table grows monotonically without this; in pilot scale ~10k
    rows/day on a busy salon is realistic, so a daily prune keeps the
    table small and the index fast.
    """
    from .models import IdempotencyKey
    deleted, _ = IdempotencyKey.objects.filter(
        expires_at__lte=timezone.now(),
    ).delete()
    if deleted:
        logger.info("idempotency.purged_expired count=%d", deleted)
    return {"deleted": deleted}
