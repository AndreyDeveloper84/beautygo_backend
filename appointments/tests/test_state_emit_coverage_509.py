"""Coverage test for #509 — every booking state transition emits the
matching outbox topic via the ADR-0009 envelope.

Pre-#509, two transitions silently bypassed the outbox:
- `appointment.complete()` (model method) → no `booking.completed` row.
- payment `waiting_for_capture` webhook → appointment → CONFIRMED → no
  `booking.confirmed` row.

bot-platform consumers downstream (Gamma's #442-#447) would have
silently missed two state transitions, drifting AI memory about user
bookings. This test pins the contract: each transition that has a
`BOOKING_*` topic in the enum MUST produce exactly one OutboxEvent row
of that topic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


# --- fixtures ---------------------------------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="sec_spec", password="x", role="specialist",
        phone="+79991005509",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "509 Specialist"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="509 Cat", slug="509-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="509 Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="sec_client", password="x", role="client",
        phone="+79991005510",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _confirmed_appointment(client_user, specialist, service):
    """Create an Appointment + Payment via the real booking service,
    then force-flip to CONFIRMED so `complete()` can run on it."""
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3),
        idempotency_key=str(uuid4()),
    )
    appt, payment = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    # Drop the booking.created row so the test-side assertions only see
    # the topic under test.
    OutboxEvent.objects.filter(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
    ).delete()
    return appt, payment


# --- Gap-fix #1: complete() now emits booking.completed --------------

@pytest.mark.django_db
class TestBookingCompleteEmit:
    def test_complete_view_emits_booking_completed(
        self, client_user, specialist_user, specialist, service,
    ):
        appt, _ = _confirmed_appointment(client_user, specialist, service)

        client = APIClient()
        client.force_authenticate(user=specialist_user)
        response = client.post(f"/api/v1/appointments/{appt.id}/complete/")
        assert response.status_code == 200, response.data

        events = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        )
        assert events.count() == 1
        evt = events.first()
        assert evt.payload["event_name"] == "booking.completed"
        assert evt.payload["event_version"] == 1
        assert evt.payload["actor"] == "admin"  # specialist → admin
        assert evt.data["booking_id"] == str(appt.id)
        assert evt.data["client_id"] == str(client_user.id)
        assert evt.data["specialist_id"] == str(specialist.id)

    def test_complete_rolls_back_emit_on_invalid_state(
        self, client_user, specialist_user, specialist, service,
    ):
        """Atomic guarantee: a failed `complete()` (e.g. status terminal)
        must roll back the outbox emit too. Otherwise the dispatcher
        picks up a row referencing a state that never transitioned."""
        appt, _ = _confirmed_appointment(client_user, specialist, service)
        appt.status = Appointment.Status.CANCELLED
        appt.save(update_fields=["status"])

        client = APIClient()
        client.force_authenticate(user=specialist_user)
        response = client.post(f"/api/v1/appointments/{appt.id}/complete/")
        assert response.status_code == 422

        assert not OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        ).exists()


# --- Gap-fix #2: payment.waiting_for_capture emits booking.confirmed -

@pytest.mark.django_db
class TestBookingConfirmedEmit:
    def test_waiting_for_capture_emits_booking_confirmed(
        self, client_user, specialist, service,
    ):
        appt, payment = _confirmed_appointment(client_user, specialist, service)
        # Standup pre-webhook state: appointment AWAITING_PAYMENT,
        # payment PENDING with a provider_payment_id YooKassa will
        # claim in the webhook body.
        appt.status = Appointment.Status.AWAITING_PAYMENT
        appt.save(update_fields=["status"])
        payment.status = Payment.Status.PENDING
        payment.provider_payment_id = "wh_test_509"
        payment.save(update_fields=["status", "provider_payment_id"])

        mock_info = {
            "provider_payment_id": "wh_test_509",
            "status": "waiting_for_capture",
            "amount": str(payment.amount),
        }
        with patch(
            "payments.views._get_yookassa",
            return_value=MagicMock(
                get_payment_info=MagicMock(return_value=mock_info),
            ),
        ):
            anon = APIClient()
            response = anon.post(
                "/api/v1/payments/webhook/",
                {
                    "event": "payment.waiting_for_capture",
                    "object": {"id": "wh_test_509"},
                },
                format="json",
            )
        assert response.status_code == 200, response.data

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

        events = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CONFIRMED,
        )
        assert events.count() == 1
        evt = events.first()
        assert evt.payload["event_name"] == "booking.confirmed"
        assert evt.payload["event_version"] == 1
        assert evt.payload["actor"] == "system"
        assert evt.data["booking_id"] == str(appt.id)
        assert evt.data["payment_id"] == str(payment.id)


# --- Acceptance #3 — every booking-domain Topic has an emit path -----

class TestEveryBookingTopicHasEmitPath:
    """Anti-regression: if a new BOOKING_* topic is added without a
    matching emit-coverage entry below, this test fires. Forces a
    contract registry update on every new topic."""

    BOOKING_EMIT_PATHS = {
        OutboxEvent.Topic.BOOKING_CREATED: (
            "CreateBookingService._execute_atomic"
        ),
        OutboxEvent.Topic.BOOKING_CONFIRMED: (
            "payments/views.py waiting_for_capture branch (#509)"
        ),
        OutboxEvent.Topic.BOOKING_CANCELLED: (
            "CancelBookingService._execute_atomic"
        ),
        OutboxEvent.Topic.BOOKING_RESCHEDULED: (
            "RescheduleBookingService._execute_atomic"
        ),
        OutboxEvent.Topic.BOOKING_COMPLETED: (
            "AppointmentViewSet.complete (#509)"
        ),
        OutboxEvent.Topic.BOOKING_NO_SHOW: (
            "— not yet implemented (tracked in #511)"
        ),
    }

    def test_every_booking_topic_documented(self):
        booking_topics = {
            value for value, _label in OutboxEvent.Topic.choices
            if value.startswith("booking.")
        }
        documented = set(self.BOOKING_EMIT_PATHS.keys())
        missing = booking_topics - documented
        assert not missing, (
            f"Booking topics without an emit-path entry: {missing}. "
            "Add them to BOOKING_EMIT_PATHS or file a follow-up issue."
        )
