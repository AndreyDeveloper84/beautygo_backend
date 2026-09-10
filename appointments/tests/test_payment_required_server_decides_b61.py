"""B-6.1 — ``payment_required`` stops being the client's word.

The field arrives verbatim in the create request body (both
``AppointmentCreateSerializer`` and ``InternalBookingCreateSerializer``
declare it) and used to reach the write path unexamined: whatever the
caller wrote decided whether a Payment row was created and — through
``confirm_immediately``, which the views derive from the same field —
whether the booking sat waiting for money. Price and duration are taken
from the resolver and never from the caller; this one field was
re-checked on no side at all.

What the server now decides, and what it must keep NOT deciding:

* A booking recorded BY STAFF is settled off-platform by definition
  (that is the premise of the walk-in path, #1017). A caller asking for
  prepayment there asks the platform to hold the card of a customer who
  never touched the request — refused, and the refusal is named.
* A CLIENT booking keeps exactly what it asked for. There is no
  prepayment policy field anywhere in the data model, so promoting
  "no prepayment" into "awaiting payment" would be an invented rule —
  and it is the rule the pilot runs on today.

The refusal carries ONE coarse name outward and its own log event
inward, because "no Payment row because the caller wanted none" and
"no Payment row because the server overruled the caller" produce the
identical row and must not produce the identical fact.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import Appointment, OutboxEvent

MSK = ZoneInfo("Europe/Moscow")

REFUSAL_EVENT = "booking.payment_required_refused"


def _future(hour: int = 11, weeks_ahead: int = 2) -> datetime:
    """A start far enough ahead that no window or notice rule bites."""
    day = date.today() + timedelta(weeks=weeks_ahead)
    return datetime(day.year, day.month, day.day, hour, tzinfo=MSK).astimezone(
        timezone.utc
    )


def _dto(client_user, specialist, service, **kwargs):
    """Only the fields under test are named here; everything else keeps
    the DTO's own defaults, so the test cannot quietly re-state the
    contract it is checking."""
    return CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=kwargs.pop("start_at", _future()),
        idempotency_key=str(uuid4()),
        **kwargs,
    )


@contextmanager
def _capturing_the_service_log(caplog):
    """The "appointments" logger is configured ``propagate=False``
    (settings/base.py LOGGING), so caplog's root handler never sees its
    records — attach the capture handler to the service logger itself.
    Same approach as DRF-1072's override-audit test."""
    service_logger = logging.getLogger(
        "appointments.application.services.create_booking_service"
    )
    service_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=service_logger.name):
            yield
    finally:
        service_logger.removeHandler(caplog.handler)


def _refusals(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if REFUSAL_EVENT in r.getMessage()]


class TestStaffRecordedBookingCannotBePrepaid:
    def test_a_staff_caller_asking_for_prepayment_gets_none(
        self, client_user, specialist, service,
    ):
        """The whole defect in one call: staff path, ``payment_required``
        asserted by the caller. Before the server owned the decision this
        produced a pending Payment row for a walk-in and parked the
        booking in AWAITING_PAYMENT — money nobody would ever be asked
        for, on a booking the customer never made."""
        result = CreateBookingService().execute(_dto(
            client_user, specialist, service,
            payment_required=True,
            actor_role="specialist",
        ))

        booking = Appointment.objects.get(id=result.booking_id)
        assert booking.payments.count() == 0
        assert booking.status == Appointment.Status.CONFIRMED

    def test_the_salon_front_desk_is_staff_too(
        self, client_user, specialist, service,
    ):
        """DRF-1064's ``salon`` actor records a booking on the customer's
        behalf — same off-platform settlement, same refusal. The rule
        keys off "is this the client acting for themselves", not off the
        one endpoint that happened to exist first."""
        result = CreateBookingService().execute(_dto(
            client_user, specialist, service,
            payment_required=True,
            actor_role="salon",
        ))

        booking = Appointment.objects.get(id=result.booking_id)
        assert booking.payments.count() == 0
        assert booking.status == Appointment.Status.CONFIRMED


class TestTheRefusalIsNamedAndCountedApart:
    def test_the_refusal_names_itself_and_says_what_was_asked(
        self, client_user, specialist, service, caplog,
    ):
        with _capturing_the_service_log(caplog):
            CreateBookingService().execute(_dto(
                client_user, specialist, service,
                payment_required=True,
                actor_role="specialist",
            ))

        refusals = _refusals(caplog)
        assert len(refusals) == 1
        message = refusals[0].getMessage()
        assert "reason=staff_recorded" in message
        assert "requested=True" in message
        assert "applied=False" in message

    def test_a_caller_who_wanted_no_payment_is_not_counted_as_refused(
        self, client_user, specialist, service, caplog,
    ):
        """The separate counter earns its keep here. This booking and the
        refused one above end up as the same row — CONFIRMED, no Payment.
        Only the counter tells them apart, and an agreement must never
        inflate it."""
        with _capturing_the_service_log(caplog):
            CreateBookingService().execute(_dto(
                client_user, specialist, service,
                payment_required=False,
                confirm_immediately=True,
                actor_role="specialist",
            ))

        assert _refusals(caplog) == []

    def test_the_created_event_carries_the_coarse_name_only_when_refused(
        self, client_user, specialist, service,
    ):
        CreateBookingService().execute(_dto(
            client_user, specialist, service,
            payment_required=True,
            actor_role="specialist",
        ))

        event = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED)
        assert event.data["payment_required_refused"] is True

    def test_an_ordinary_booking_payload_stays_byte_identical(
        self, client_user, specialist, service,
    ):
        CreateBookingService().execute(_dto(
            client_user, specialist, service,
            payment_required=False,
            confirm_immediately=True,
            actor_role="specialist",
        ))

        event = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED)
        assert "payment_required_refused" not in event.data


class TestTheClientPathIsUntouched:
    def test_a_client_asking_for_prepayment_still_gets_it(
        self, client_user, specialist, service, caplog,
    ):
        """Inertness pin. The customer contract is the one thing this
        change must not move: Payment row, AWAITING_PAYMENT, no refusal."""
        with _capturing_the_service_log(caplog):
            result = CreateBookingService().execute(_dto(
                client_user, specialist, service,
                payment_required=True,
                actor_role="user",
            ))

        booking = Appointment.objects.get(id=result.booking_id)
        assert booking.payments.count() == 1
        assert booking.payments.get().status == "pending"
        assert booking.status == Appointment.Status.AWAITING_PAYMENT
        assert _refusals(caplog) == []

    def test_the_pilot_no_prepayment_client_booking_is_not_promoted(
        self, client_user, specialist, service, caplog,
    ):
        """The bot's pilot baseline: the customer books without
        prepayment. The server owns the decision now, but it owns no
        policy that could turn this back into an awaited payment — and
        must not invent one."""
        with _capturing_the_service_log(caplog):
            result = CreateBookingService().execute(_dto(
                client_user, specialist, service,
                payment_required=False,
                confirm_immediately=True,
                actor_role="user",
            ))

        booking = Appointment.objects.get(id=result.booking_id)
        assert booking.payments.count() == 0
        assert booking.status == Appointment.Status.CONFIRMED
        assert _refusals(caplog) == []
