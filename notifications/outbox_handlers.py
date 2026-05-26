"""Outbox event handlers — wire ``OutboxEvent`` topics to NotificationService.

The transactional-outbox dispatcher in ``appointments.tasks`` reads pending
events every 10s and routes them by topic. Each function in this module is
a topic handler: it loads the relevant domain object, builds the template
context, and calls ``NotificationService().send()`` once per recipient.

Design notes:

- **Two recipients per booking event.** Most booking topics fire pushes to
  both the client and the specialist. We send two separate Notification
  rows (and two separate device-token lookups) rather than one shared row,
  so the audit trail and read state are per-user.
- **Templates may be missing.** Slice N4 adds the rescheduled / completed
  / no_show / specialist-cancelled templates. Until then a
  ``KeyError(template_id)`` from ``templates.get_template`` would make the
  whole outbox dispatcher fail and retry forever. The handlers catch
  KeyError per-template, log, and continue — the row is marked processed
  even with partial delivery so retry storms don't pile up. Slice N4
  fills the gaps.
- **Loading the appointment.** The cancellation event payload omits
  ``client_id`` (see ``cancel_reschedule_service.py:104``), so handlers
  reload the Appointment by ``booking_id`` rather than trusting the
  payload to be complete.
- **Soft-delete on missing FK.** If the appointment was hard-deleted
  between event write and dispatch (rare), we log and skip rather than
  raise — the dispatcher's error_count would block the row otherwise.

Registration happens in ``appointments.tasks`` at module load via
``EVENT_HANDLERS.update(BOOKING_HANDLERS)``. Adding a new topic? Define
the handler here, append to ``BOOKING_HANDLERS``, write a test.
"""
from __future__ import annotations

import logging
from typing import Callable

from django.core.exceptions import ValidationError

from appointments.models import Appointment, OutboxEvent

from .models import Notification
from .services.dispatcher import NotificationService
from .templates import TEMPLATES

logger = logging.getLogger(__name__)

EventHandler = Callable[[OutboxEvent], None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_appointment(event: OutboxEvent) -> Appointment | None:
    """Reload the Appointment referenced by the event.

    Returns None and logs (rather than raising) for any of these
    non-transient data issues:
    - ``booking_id`` missing from payload (writer bug)
    - row deleted between event write and dispatch (admin cleanup)
    - ``booking_id`` not a valid UUID (data drift / legacy event)

    The dispatcher's exception path is reserved for transient failures
    (DB blip, downstream service down) where a retry has a chance to
    succeed. Bad payloads aren't transient — letting them raise would
    pin the OutboxEvent at error_count++ until it dead-letters.
    """
    booking_id = event.data.get("booking_id")
    if not booking_id:
        logger.warning(
            "outbox.handler.missing_booking_id topic=%s event_id=%s",
            event.topic, event.id,
        )
        return None
    try:
        return (
            Appointment.objects
            .select_related("client", "specialist", "service")
            .get(id=booking_id)
        )
    except (
        Appointment.DoesNotExist, ValueError, TypeError, ValidationError,
    ) as exc:
        logger.warning(
            "outbox.handler.appointment_missing topic=%s booking_id=%s err=%s",
            event.topic, booking_id, exc,
        )
        return None


def _client_name(appointment: Appointment) -> str:
    """Best-effort display name for the client side of templates.

    Falls back to first name then "клиент" so a missing profile row never
    produces a literally empty push body.
    """
    profile = getattr(appointment.client, "profile", None)
    if profile and profile.full_name:
        return profile.full_name
    first = getattr(appointment.client, "first_name", "") or ""
    return first or "клиент"


def _appointment_context(appointment: Appointment) -> dict:
    """Shared template context for booking-related notifications.

    Mirrors the keys used by the existing ``appointment_reminder_1h``
    template (`notifications/tasks.py:96`) so all booking templates can
    expand against the same dict without per-template massaging.
    """
    return {
        "appointment_id": str(appointment.id),
        "service_name": appointment.service.name if appointment.service_id else "",
        "specialist_name": (
            appointment.specialist.display_name
            if appointment.specialist_id else ""
        ),
        "client_name": _client_name(appointment),
        "date_time": appointment.start_datetime.strftime("%H:%M %d.%m"),
        "address": (
            getattr(appointment.specialist, "address", "") or ""
            if appointment.specialist_id else ""
        ),
    }


def _send_if_template_exists(
    *, user, template_id: str, context: dict, event_id,
) -> None:
    """Call NotificationService.send only if the template is registered.

    Slice N4 fills the catalogue. Until then unknown ids log + skip
    rather than fail the outbox row.
    """
    if template_id not in TEMPLATES:
        logger.info(
            "outbox.handler.template_pending template=%s event_id=%s",
            template_id, event_id,
        )
        return
    NotificationService().send(
        user=user, template_id=template_id, context=context,
    )


# ---------------------------------------------------------------------------
# Booking handlers
# ---------------------------------------------------------------------------


def handle_booking_created(event: OutboxEvent) -> None:
    """Notify both client and specialist when a booking is created."""
    appointment = _load_appointment(event)
    if appointment is None:
        return
    ctx = _appointment_context(appointment)

    _send_if_template_exists(
        user=appointment.client,
        template_id="appointment_created_client",
        context=ctx,
        event_id=event.id,
    )
    _send_if_template_exists(
        user=appointment.specialist.user,
        template_id="appointment_created_specialist",
        context=ctx,
        event_id=event.id,
    )


def handle_booking_confirmed(event: OutboxEvent) -> None:
    """Confirmation push to the client.

    Today the pending → confirmed transition fires automatically on
    payment, which we already cover with payment_paid. Until specialists
    can manually confirm (out of M3 scope) this is a no-op so the topic
    has a registered handler.
    """
    appointment = _load_appointment(event)
    if appointment is None:
        return
    _send_if_template_exists(
        user=appointment.client,
        template_id="appointment_confirmed_client",
        context=_appointment_context(appointment),
        event_id=event.id,
    )


def handle_booking_cancelled(event: OutboxEvent) -> None:
    """Notify both sides when a booking is cancelled.

    Customer-side template branches on ``reason``:

    - ``specialist_departure`` (#246 Q2 cascade): use the dedicated
      ``appointment_cancelled_specialist_departure`` template — the
      copy carries the salon's commitment to reach back out
      ('обещает связаться'), distinct from generic cancellation.
    - everything else: standard ``appointment_cancelled`` template.

    Specialist-side push is suppressed for ``specialist_departure``
    — the specialist already knows (they were just revoked) and a
    push would be confusing noise.
    """
    appointment = _load_appointment(event)
    if appointment is None:
        return
    ctx = _appointment_context(appointment)

    reason = event.data.get("reason") if event.data else None
    if reason == "specialist_departure":
        _send_if_template_exists(
            user=appointment.client,
            template_id="appointment_cancelled_specialist_departure",
            context=ctx,
            event_id=event.id,
        )
        # No specialist-side push — they were just revoked.
        return

    _send_if_template_exists(
        user=appointment.client,
        template_id="appointment_cancelled",
        context=ctx,
        event_id=event.id,
    )
    _send_if_template_exists(
        user=appointment.specialist.user,
        template_id="appointment_cancelled_specialist",
        context=ctx,
        event_id=event.id,
    )


def handle_booking_rescheduled(event: OutboxEvent) -> None:
    """Notify both sides when a booking moves to a new time."""
    appointment = _load_appointment(event)
    if appointment is None:
        return
    ctx = _appointment_context(appointment)
    # The reschedule payload carries the previous start so the push body
    # can read "перенесена с 14:00 → 16:00". Surface it in the context;
    # templates that don't reference it ignore the extra key.
    old_start_at = event.data.get("old_start_at")
    if old_start_at:
        # Lazy import to avoid django.utils.dateparse cost at module load.
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(old_start_at)
        if parsed is not None:
            ctx["old_date_time"] = parsed.strftime("%H:%M %d.%m")

    _send_if_template_exists(
        user=appointment.client,
        template_id="appointment_rescheduled_client",
        context=ctx,
        event_id=event.id,
    )
    _send_if_template_exists(
        user=appointment.specialist.user,
        template_id="appointment_rescheduled_specialist",
        context=ctx,
        event_id=event.id,
    )


def handle_booking_completed(event: OutboxEvent) -> None:
    """Send the review-request push to the client.

    The transition to completed has no writer in the codebase yet (it's
    expected to come from a Slice N5 specialist-side action). The
    handler is still registered so the topic isn't a stub when that
    writer lands.
    """
    appointment = _load_appointment(event)
    if appointment is None:
        return
    _send_if_template_exists(
        user=appointment.client,
        template_id="review_request",
        context=_appointment_context(appointment),
        event_id=event.id,
    )


def handle_booking_no_show(event: OutboxEvent) -> None:
    """Notify the specialist when a client doesn't show up."""
    appointment = _load_appointment(event)
    if appointment is None:
        return
    _send_if_template_exists(
        user=appointment.specialist.user,
        template_id="booking_no_show_specialist",
        context=_appointment_context(appointment),
        event_id=event.id,
    )


# ---------------------------------------------------------------------------
# Payment handlers
# ---------------------------------------------------------------------------


def _load_payment(event: OutboxEvent):
    """Reload the Payment + appointment context referenced by the event.

    Same defensive shape as ``_load_appointment``: returns None and logs
    on missing/malformed data so the dispatcher doesn't retry forever.
    """
    from payments.models import Payment  # lazy: avoid import at module load

    payment_id = event.data.get("payment_id")
    if not payment_id:
        logger.warning(
            "outbox.handler.missing_payment_id topic=%s event_id=%s",
            event.topic, event.id,
        )
        return None
    try:
        return (
            Payment.objects
            .select_related(
                "appointment__client",
                "appointment__specialist",
                "appointment__service",
            )
            .get(id=payment_id)
        )
    except (Payment.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        logger.warning(
            "outbox.handler.payment_missing topic=%s payment_id=%s err=%s",
            event.topic, payment_id, exc,
        )
        return None


def _payment_context(payment, event: OutboxEvent) -> dict:
    """Template context for payment notifications. The amount in the
    payload may be the full payment or a partial refund — the
    PAYMENT_REFUNDED writer puts the refunded amount there, the
    PAYMENT_CONFIRMED writer puts the captured amount."""
    appointment = payment.appointment
    return {
        "payment_id": str(payment.id),
        "appointment_id": str(appointment.id),
        "amount": str(event.data.get("amount") or payment.amount),
        "service_name": (
            appointment.service.name
            if appointment.service_id else ""
        ),
        "specialist_name": (
            appointment.specialist.display_name
            if appointment.specialist_id else ""
        ),
        "is_partial": event.data.get("is_partial", False),
    }


def handle_payment_confirmed(event: OutboxEvent) -> None:
    """Push the 'оплата прошла' notification to the client."""
    payment = _load_payment(event)
    if payment is None:
        return
    _send_if_template_exists(
        user=payment.appointment.client,
        template_id="payment_paid",
        context=_payment_context(payment, event),
        event_id=event.id,
    )


def handle_payment_refunded(event: OutboxEvent) -> None:
    """Push the refund-confirmation notification to the client.

    Template ``payment_refunded`` lands in Slice N4 — until then this
    is a logged no-op so the topic isn't an unknown_topic error.
    """
    payment = _load_payment(event)
    if payment is None:
        return
    _send_if_template_exists(
        user=payment.appointment.client,
        template_id="payment_refunded",
        context=_payment_context(payment, event),
        event_id=event.id,
    )


# ---------------------------------------------------------------------------
# Registry — appointments.tasks imports this and merges into EVENT_HANDLERS
# ---------------------------------------------------------------------------


BOOKING_HANDLERS: dict[str, EventHandler] = {
    OutboxEvent.Topic.BOOKING_CREATED: handle_booking_created,
    OutboxEvent.Topic.BOOKING_CONFIRMED: handle_booking_confirmed,
    OutboxEvent.Topic.BOOKING_CANCELLED: handle_booking_cancelled,
    OutboxEvent.Topic.BOOKING_RESCHEDULED: handle_booking_rescheduled,
    OutboxEvent.Topic.BOOKING_COMPLETED: handle_booking_completed,
    OutboxEvent.Topic.BOOKING_NO_SHOW: handle_booking_no_show,
    OutboxEvent.Topic.PAYMENT_CONFIRMED: handle_payment_confirmed,
    OutboxEvent.Topic.PAYMENT_REFUNDED: handle_payment_refunded,
}


# Re-export for tests that want to assert "Notification was sent for X".
__all__ = [
    "BOOKING_HANDLERS",
    "handle_booking_created",
    "handle_booking_confirmed",
    "handle_booking_cancelled",
    "handle_booking_rescheduled",
    "handle_booking_completed",
    "handle_booking_no_show",
    "handle_payment_confirmed",
    "handle_payment_refunded",
    "Notification",  # convenience for test imports
]
