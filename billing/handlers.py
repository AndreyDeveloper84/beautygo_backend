"""Outbox event handlers for billing — wired by W1's registry patch.

R-5 handoff: W1 adds one line to ``appointments/tasks.py``
``EVENT_HANDLERS`` mapping ``OutboxEvent.Topic.BOOKING_COMPLETED`` to
``billing.handlers.on_booking_completed``. Until then tests call the
handler directly.

Signature follows the ``EventHandler = Callable[[OutboxEvent], None]``
convention (see notifications/outbox_handlers.py).
"""
from __future__ import annotations

import logging

import sentry_sdk

from appointments.models import Appointment, OutboxEvent
from billing.services import accrue_booking_fee

logger = logging.getLogger(__name__)


def on_booking_completed(event: OutboxEvent) -> None:
    """Accrue BookingFee for a completed appointment (AYLA-DEC-0010).

    Non-transient data issues (missing/invalid appointment_id, deleted
    row) are logged and swallowed — the dispatcher's retry path is for
    transient failures only (mirrors _load_appointment in
    notifications/outbox_handlers.py).
    """
    appointment_id = (event.data or {}).get("appointment_id")
    if not appointment_id:
        logger.error("billing.completed.no_appointment_id event_id=%s", event.id)
        return
    appointment = (
        Appointment.objects
        .select_related("specialist")
        .filter(pk=appointment_id)
        .first()
    )
    if appointment is None:
        logger.error(
            "billing.completed.appointment_missing id=%s event_id=%s",
            appointment_id, event.id,
        )
        return
    try:
        accrue_booking_fee(appointment)
    except Exception as exc:  # fee accrual must not break the dispatcher
        logger.exception(
            "billing.completed.accrual_failed appointment_id=%s", appointment_id,
        )
        sentry_sdk.capture_exception(exc)
