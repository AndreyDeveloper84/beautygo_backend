"""
Outbox worker — processes OutboxEvent records asynchronously.

For MVP: this module is included but the Celery task is NOT scheduled.
Events accumulate in the DB and can be processed manually or when
Celery/Redis infrastructure is set up.

To activate: add to Celery beat schedule:
    "process-outbox-events": {
        "task": "appointments.infrastructure.outbox_worker.process_outbox_events",
        "schedule": 5.0,
    }
"""

from __future__ import annotations

import logging
from datetime import date

from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BATCH_SIZE = 50


def process_outbox_events() -> dict:
    """
    Process pending outbox events.

    Can be called as a Celery task or invoked manually:
        from appointments.infrastructure.outbox_worker import process_outbox_events
        result = process_outbox_events()
    """
    from appointments.models import OutboxEvent

    pending = (
        OutboxEvent.objects
        .filter(processed_at__isnull=True, error_count__lt=MAX_RETRIES)
        .order_by('created_at')[:BATCH_SIZE]
    )

    processed = 0
    failed = 0

    for event in pending:
        try:
            handler = HANDLERS.get(event.topic)
            if handler:
                handler(event.payload)
            event.processed_at = timezone.now()
            event.save(update_fields=['processed_at'])
            processed += 1
        except Exception as e:
            logger.error(
                "outbox.handler_error topic=%s event_id=%s error=%s",
                event.topic, event.id, str(e), exc_info=True,
            )
            event.error_count += 1
            event.last_error = str(e)[:1000]
            event.save(update_fields=['error_count', 'last_error'])
            failed += 1

    if processed or failed:
        logger.info(
            "outbox.batch_complete processed=%d failed=%d", processed, failed,
        )

    return {"processed": processed, "failed": failed}


# --- Handlers (stubs for MVP) ---

def handle_booking_created(payload: dict) -> None:
    """Stub: would create payment intent + send push to specialist."""
    logger.info("outbox.handle booking.created appointment_id=%s", payload.get("appointment_id"))
    _invalidate_slots_from_payload(payload)


def handle_booking_cancelled(payload: dict) -> None:
    """Stub: would process refund + send notifications."""
    logger.info("outbox.handle booking.cancelled appointment_id=%s", payload.get("appointment_id"))
    _invalidate_slots_from_payload(payload)


def handle_booking_rescheduled(payload: dict) -> None:
    """Invalidate cache for BOTH old and new dates."""
    logger.info("outbox.handle booking.rescheduled appointment_id=%s", payload.get("appointment_id"))
    _invalidate_slots_from_payload(payload)
    if "old_start_at" in payload:
        old_date = date.fromisoformat(payload["old_start_at"][:10])
        from appointments.infrastructure.cache.slot_cache import SlotCacheService
        SlotCacheService().invalidate(payload["specialist_id"], old_date)


def handle_booking_confirmed(payload: dict) -> None:
    """Stub: would send confirmation notifications."""
    logger.info("outbox.handle booking.confirmed appointment_id=%s", payload.get("appointment_id"))


def handle_payment_captured(payload: dict) -> None:
    """Stub: would transition booking to COMPLETED-billing.

    Renamed from handle_payment_confirmed in B-1a (Variant C).
    """
    logger.info("outbox.handle payment.captured payment_id=%s", payload.get("payment_id"))


def handle_payment_failed(payload: dict) -> None:
    """Stub: B-1b — payment failure. The retry threshold + customer DM
    live on the bot side (W2); Ayla side is log-only."""
    logger.info(
        "outbox.handle payment.failed payment_id=%s reason=%s",
        payload.get("payment_id"),
        payload.get("reason_code") or "",
    )


def handle_cache_invalidate_slots(payload: dict) -> None:
    """Direct cache invalidation."""
    from appointments.infrastructure.cache.slot_cache import SlotCacheService
    specialist_id = payload["specialist_id"]
    target_date = date.fromisoformat(payload["date"])
    SlotCacheService().invalidate(specialist_id, target_date)


def _invalidate_slots_from_payload(payload: dict) -> None:
    """Helper: invalidate slot cache based on start_at in payload."""
    start_at = payload.get("start_at")
    specialist_id = payload.get("specialist_id")
    if start_at and specialist_id:
        target_date = date.fromisoformat(start_at[:10])
        from appointments.infrastructure.cache.slot_cache import SlotCacheService
        SlotCacheService().invalidate(specialist_id, target_date)


HANDLERS = {
    "booking.created": handle_booking_created,
    "booking.cancelled": handle_booking_cancelled,
    "booking.rescheduled": handle_booking_rescheduled,
    "booking.confirmed": handle_booking_confirmed,
    "payment.captured": handle_payment_captured,
    "payment.failed": handle_payment_failed,
    "cache.invalidate_slots": handle_cache_invalidate_slots,
}
