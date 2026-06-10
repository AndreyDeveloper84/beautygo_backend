"""Emitter conformance against the locked bot ingest contract (#196).

ai-bot-platform's ingest path is frozen (PR #1003-era): the parse-level
allowlist ``apps/eventbus/ingest_envelope.ALLOWED_EVENT_NAMES`` rejects any
``event_name`` outside the closed set with a 422 → DLQ, and the booking
consumers (``apps/eventbus/consumers/booking.py``) hard-read specific
``data`` keys (``KeyError`` → handler 500, the event never lands).

This Ayla-side test pins the *emitter* so a future payload refactor cannot
silently drift the cross-service stream back into the DLQ. It encodes the
two contract facts that bit us:

1. **Taxonomy.** Every externally-delivered topic MUST be a member of the
   bot allowlist. ``booking.no_show`` is deliberately NOT in that set —
   event-contract.md §3.2 models a no-show as ``booking.cancelled`` +
   ``reason_code="user_no_show"`` — so it is the one topic kept for
   internal handlers only and never shipped under its own name.
2. **Payload shape.** booking.created carries ``status`` (consumer hard
   key); booking.rescheduled carries ``new_start_at``; the cancellation
   vocabulary (``cancelled_by``/``reason_code``/``cancelled_at``) is
   present on every cancel — including the no-show-derived one.

The bot allowlist is mirrored here as a literal so a divergence on either
side trips this test rather than slipping into production as a silent DLQ.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import (
    CancelBookingDTO,
    CreateBookingDTO,
    RescheduleBookingDTO,
)
from appointments.application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


# Mirror of ai-bot-platform apps/eventbus/ingest_envelope.ALLOWED_EVENT_NAMES
# (== ingest_dispatcher._KNOWN_NAMES). Any topic Ayla delivers externally
# must be in here or the bot dead-letters it. booking.no_show is absent on
# purpose (see module docstring).
BOT_ALLOWED_EVENT_NAMES = frozenset(
    {
        "booking.created",
        "booking.confirmed",
        "booking.cancelled",
        "booking.rescheduled",
        "booking.completed",
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "payment.refunded",
        "review.created",
        "service.updated",
        "master.schedule.updated",
        "user.profile.updated",
    }
)

# Topics Ayla emits but keeps for in-process handlers only — never shipped
# to the bot under this name.
INTERNAL_ONLY_TOPICS = frozenset({"booking.no_show"})


# --- fixtures (mirror the #509/#511 setup) ---------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="conf196_spec", password="x", role="specialist",
        phone="+79991009601",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "196 Conformance"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="196 Cat", slug="196-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="196 Service",
        price=Decimal("1800.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="conf196_client", password="x", role="client",
        phone="+79991009602",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _create_booking(client_user, specialist, service, *, start_in_hours: int = 3):
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(start_in_hours),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    return appt


def _confirmed(client_user, specialist, service, *, start_in_hours: int = 3):
    appt = _create_booking(
        client_user, specialist, service, start_in_hours=start_in_hours,
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    return appt


# --- taxonomy: no topic Ayla ships can dead-letter -------------------------

class TestTaxonomyConformance:
    def test_every_external_topic_is_in_bot_allowlist(self):
        """Each OutboxEvent.Topic destined for the bot must be a name the
        bot's parse-level allowlist accepts. The only declared topic
        allowed to be absent is booking.no_show (internal-only)."""
        declared = {value for value, _label in OutboxEvent.Topic.choices}
        booking_payment = {
            t for t in declared
            if t.startswith("booking.") or t.startswith("payment.")
        }
        offenders = booking_payment - BOT_ALLOWED_EVENT_NAMES - INTERNAL_ONLY_TOPICS
        assert not offenders, (
            f"OutboxEvent.Topic declares booking/payment topics the bot "
            f"ingest allowlist rejects (→ 422/DLQ): {sorted(offenders)}. "
            "Either the name drifted or it must be marked internal-only."
        )

    def test_no_show_is_not_a_cross_service_name(self):
        """Guard the modeling decision: booking.no_show must never be in
        the bot allowlist — a no-show ships as booking.cancelled."""
        assert "booking.no_show" not in BOT_ALLOWED_EVENT_NAMES


# --- payload shape: consumer hard-keys present -----------------------------

@pytest.mark.django_db
class TestBookingCreatedPayload:
    def test_created_carries_consumer_required_keys(
        self, client_user, specialist, service,
    ):
        appt = _create_booking(client_user, specialist, service)
        evt = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED)
        assert evt.payload["event_name"] == "booking.created"
        assert evt.payload["event_version"] == 1
        # Hard keys the bot's consumers/booking.py reads without .get().
        for key in ("appointment_id", "start_at", "end_at", "status"):
            assert key in evt.data, f"booking.created missing {key!r}"
        assert evt.data["status"] == str(appt.status)
        # Contract §3.1 fields the consumer stores on the proxy.
        assert evt.data["source"] in {"mobile_app", "walk_in"}
        assert evt.data["price_total"] == "1800.00"


@pytest.mark.django_db
class TestBookingRescheduledPayload:
    def test_reschedule_carries_new_start_at(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(
            client_user, specialist, service, start_in_hours=48,
        )
        OutboxEvent.objects.all().delete()
        new_start = _future_utc(72)
        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        ))
        evt = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_RESCHEDULED)
        # Consumer hard-reads data["new_start_at"]; the in-process cache
        # handler reads data["start_at"]. Both must be present.
        assert evt.data["new_start_at"] == new_start.isoformat()
        assert evt.data["start_at"] == new_start.isoformat()


@pytest.mark.django_db
class TestBookingCancelledPayload:
    def test_cancel_carries_cancellation_vocabulary(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        OutboxEvent.objects.all().delete()
        CancelBookingService().execute(CancelBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            initiator_role="client",
        ))
        evt = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CANCELLED)
        assert evt.data["cancelled_by"] == "user"
        assert evt.data["reason_code"] == "user_changed_plans"
        assert "cancelled_at" in evt.data


# --- the modeling decision: no-show ships as booking.cancelled -------------

@pytest.mark.django_db
class TestNoShowEmitsConformantCancelled:
    def test_no_show_keeps_internal_and_emits_cancelled(
        self, client_user, specialist_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        OutboxEvent.objects.all().delete()

        client = APIClient()
        client.defaults["HTTP_X_APP_TYPE"] = "pro"
        client.force_authenticate(user=specialist_user)
        r = client.post(f"/api/v1/appointments/{appt.id}/no-show/")
        assert r.status_code == 200, r.data

        # Internal signal kept (internal-only topic, fed to in-process
        # handlers; never shipped to the bot under this name).
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        ).count() == 1

        # Cross-service representation: booking.cancelled + user_no_show.
        cancelled = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        )
        assert cancelled.payload["event_name"] == "booking.cancelled"
        assert cancelled.payload["event_name"] in BOT_ALLOWED_EVENT_NAMES
        assert cancelled.data["reason_code"] == "user_no_show"
        assert cancelled.data["cancelled_by"] == "master"
        assert cancelled.data["appointment_id"] == str(appt.id)
        # Envelope user_id is the customer (their booking), not the
        # specialist who marked the no-show.
        assert cancelled.payload["user_id"] == str(client_user.id)
