"""Closing a visit — one implementation, several callers (DRF-1064).

Three call sites close bookings: the HTTP action (specialist or salon
administrator), the periodic sweep that closes what nobody got round to,
and the one-shot command that drains the historical tail. They must
produce byte-identical facts — the same state transition, the same
``booking.completed`` payload — or a consumer will see a booking closed
by the sweep behave differently from one closed at the front desk.

So the payload lives here, in one place, and the differences between
callers stay where they belong: authorisation and optimistic concurrency
in the view, batching and eligibility in the task.

**Locking is the caller's job.** Every function here assumes the row is
already locked (``select_for_update``) inside an open transaction. That
is not laziness — the lock has to be taken together with whatever else
the caller is serialising against, and taking it twice would be worse
than not taking it here at all.
"""
from __future__ import annotations

import logging

from appointments.domain.value_objects import envelope_actor_for

logger = logging.getLogger(__name__)


def emit_booking_completed(appointment, *, completed_by: str):
    """Write the ``booking.completed`` outbox row for a closed booking.

    Call AFTER ``Appointment.complete()`` — the payload reads
    ``completed_at`` off the row so the event timestamp and the stored
    one are the same instant rather than two calls to ``now()``.
    """
    from appointments.infrastructure.outbox import (
        emit_outbox_event, safe_tenant_id,
    )
    from appointments.models import OutboxEvent

    return emit_outbox_event(
        topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        data={
            "appointment_id": str(appointment.id),
            "client_id": str(appointment.client_id),
            "specialist_id": str(appointment.specialist_id),
            # Contract §3.4 field the consumer reads for the completion
            # timestamp (consumers/booking.py: data.get("completed_at")).
            "completed_at": appointment.completed_at.isoformat(),
            # DRF-1064 — WHO closed the visit, from the OperationalActor
            # vocabulary. New OPTIONAL field: event-contract §4.1 lists
            # "adding a new OPTIONAL field that consumers ignore by
            # default" as non-breaking, so booking.completed stays at
            # event_version 1 and no deprecation window opens. The
            # envelope actor cannot carry this — it is a coarse
            # three-value enum by design (§2.2: "does NOT identify which
            # specific admin… it goes in data").
            "completed_by": completed_by,
        },
        # event-contract §2.2: for an admin-actor event, user_id is the
        # AFFECTED user, not the operator. The bot's consumer contract
        # (§3.4 step 2) fires the post-visit review skill at this id.
        user_id=appointment.client_id,
        tenant_id=safe_tenant_id(appointment, context="booking.completed"),
        actor=envelope_actor_for(completed_by),
    )


def close_booking(appointment, *, completed_by: str) -> None:
    """Transition a locked booking to ``completed`` and emit the fact.

    ``Appointment.complete()`` re-checks the state machine against the
    locked row, so a booking a racing transaction already closed raises
    ``ValidationError`` here rather than producing a second event.
    """
    appointment.complete(completed_by=completed_by)
    emit_booking_completed(appointment, completed_by=completed_by)


def emit_booking_no_show(appointment, *, marked_by: str):
    """Write the two outbox rows a no-show produces (DRF-1851, K8).

    Call AFTER ``Appointment.mark_no_show()``. Two rows, on purpose:

    * ``booking.no_show`` — internal-delivery only (#511 reliability
      scoring, revenue-loss tracking); the topic is not in the
      cross-service taxonomy and would dead-letter at the bot;
    * ``booking.cancelled`` + ``reason_code="user_no_show"`` — the
      cross-service representation (event-contract §3.2): the bot's
      consumer flips its mirror to cancelled/no-show and cancels pending
      reminders. ``user_id`` is the customer whose booking this is, not
      the operator who marked it (§2.2).

    Lives here — next to ``emit_booking_completed`` — so the mobile action
    and the salon surface produce byte-identical facts; before this the
    payload was built inline in the mobile view and the salon route was
    deliberately left without no-show rather than copy sixty lines.
    """
    from django.utils import timezone

    from appointments.domain.value_objects import cancelled_by_for
    from appointments.infrastructure.outbox import (
        emit_outbox_event, safe_tenant_id,
    )
    from appointments.models import OutboxEvent

    emit_outbox_event(
        topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        data={
            "appointment_id": str(appointment.id),
            "client_id": str(appointment.client_id),
            "specialist_id": str(appointment.specialist_id),
            # Attribution counterpart of completed_by; the Domain Event
            # Registry reserves an optional `marked_by` on
            # appointment.no_show — this is the value behind it.
            "no_show_marked_by": marked_by,
        },
        user_id=appointment.client_id,
        tenant_id=safe_tenant_id(appointment, context="booking.no_show"),
        actor=envelope_actor_for(marked_by),
    )
    return emit_outbox_event(
        topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        data={
            "appointment_id": str(appointment.id),
            "specialist_id": str(appointment.specialist_id),
            "start_at": appointment.start_datetime.isoformat(),
            # §3.2 vocabulary {user, admin, master, system}.
            "cancelled_by": cancelled_by_for(marked_by),
            "reason_code": "user_no_show",
            "cancelled_at": timezone.now().isoformat(),
        },
        user_id=appointment.client_id,
        tenant_id=safe_tenant_id(appointment, context="booking.cancelled"),
        actor=envelope_actor_for(marked_by),
    )


def mark_booking_no_show(appointment, *, marked_by: str) -> None:
    """Transition a locked booking to ``no_show`` and emit the facts.

    Sibling of :func:`close_booking`: ``Appointment.mark_no_show()``
    re-checks the state machine against the locked row, so a booking a
    racing transaction already settled raises ``ValidationError`` here
    rather than producing a second pair of events.
    """
    appointment.mark_no_show(marked_by=marked_by)
    emit_booking_no_show(appointment, marked_by=marked_by)


def schedule_capture_safely(appointment) -> None:
    """Kick off the two-stage payment capture for a closed booking.

    Deliberately swallows every exception. The booking is already
    durably completed by the time this runs; a broker or provider hiccup
    must not surface as a failure of a fact that has already happened.
    The reconciliation job and the ``retry_capture`` command pick up
    whatever this misses.

    This exists as a shared helper because the capture hook lives in the
    HTTP view rather than in a ``booking.completed`` handler — meaning
    every non-HTTP path to completion has to remember it by hand. The
    sweep would otherwise silently leave held payments uncaptured.
    """
    from payments.services import schedule_capture_for_appointment

    try:
        schedule_capture_for_appointment(
            appointment, completed_at=appointment.completed_at,
        )
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception(
            'capture.schedule_failed appointment_id=%s', appointment.id,
        )
