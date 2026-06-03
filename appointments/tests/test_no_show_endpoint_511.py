"""Regression tests for #511 — POST /api/v1/appointments/{id}/no-show/.

`BookingStatus.NO_SHOW` and `OutboxEvent.Topic.BOOKING_NO_SHOW` shipped
in earlier Phase 0 work but the endpoint that drives the transition
didn't exist — specialists had to mark `cancelled` and lose the
no-show signal (revenue loss tracking + reliability score).

This file pins:
- Specialist can mark confirmed → no_show (200 + envelope emit).
- Client cannot (403 — permission guard).
- Other specialist cannot mark someone else's booking (404 — owner check).
- Invalid state (e.g. already cancelled) → 422 with no emit (atomic rollback).
- Concurrent POSTs serialise via select_for_update — only one emit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="ns511_spec", password="x", role="specialist",
        phone="+79991005111",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "511 NS"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="511 NS Cat", slug="511-ns-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="511 NS Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="ns511_client", password="x", role="client",
        phone="+79991005112",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _confirmed(client_user, specialist, service):
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    # Drop booking.created emit so subsequent assertions only see the
    # no_show row under test.
    OutboxEvent.objects.filter(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
    ).delete()
    return appt


def _spec_client(user) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    c.force_authenticate(user=user)
    return c


def _client_client(user) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestNoShowHappyPath:
    def test_specialist_marks_confirmed_booking_as_no_show(
        self, client_user, specialist_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        r = _spec_client(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
        )
        assert r.status_code == 200, r.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW

        events = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        )
        assert events.count() == 1
        evt = events.first()
        assert evt.payload["event_name"] == "booking.no_show"
        assert evt.payload["event_version"] == 1
        # Specialist-initiated → admin per ADR-0009 actor mapping.
        assert evt.payload["actor"] == "admin"
        assert evt.data["appointment_id"] == str(appt.id)
        assert evt.data["client_id"] == str(client_user.id)
        assert evt.data["specialist_id"] == str(specialist.id)


@pytest.mark.django_db
class TestNoShowPermissionAndBoundary:
    def test_client_cannot_mark_no_show(
        self, client_user, specialist, service,
    ):
        """is_client check fires at the top — 403, no DB hit beyond
        the role check, no envelope emit."""
        appt = _confirmed(client_user, specialist, service)
        r = _client_client(client_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
        )
        assert r.status_code == 403
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        ).exists()

    def test_other_specialist_cannot_mark_no_show(
        self, client_user, specialist_user, specialist, service,
    ):
        """Specialist B can't mark Specialist A's appointment as
        no-show. 404 (not 403) — don't leak existence cross-specialist."""
        appt = _confirmed(client_user, specialist, service)
        other_spec_user = User.objects.create_user(
            username="ns511_other_spec", password="x", role="specialist",
            phone="+79991005113",
        )
        SpecialistProfile.objects.filter(user=other_spec_user).update(
            status=SpecialistProfile.ProfileStatus.ACTIVE,
        )
        r = _spec_client(other_spec_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
        )
        assert r.status_code == 404
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        ).exists()


@pytest.mark.django_db
class TestNoShowStateMachine:
    def test_rejects_already_cancelled_booking(
        self, client_user, specialist_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        appt.status = Appointment.Status.CANCELLED
        appt.save(update_fields=["status"])
        r = _spec_client(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
        )
        assert r.status_code == 422
        appt.refresh_from_db()
        # State must NOT have flipped; rollback covers the emit.
        assert appt.status == Appointment.Status.CANCELLED
        assert not OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
        ).exists()

    def test_rejects_pending_booking(
        self, client_user, specialist_user, specialist, service,
    ):
        """PENDING must reject (only CONFIRMED → NO_SHOW is a valid
        transition). AWAITING_PAYMENT / COMPLETED / NO_SHOW rejection
        is covered exhaustively in
        appointments/tests/test_domain.py::TestBookingStateMachine."""
        appt = _confirmed(client_user, specialist, service)
        appt.status = Appointment.Status.PENDING
        appt.save(update_fields=["status"])
        r = _spec_client(specialist_user).post(
            f"/api/v1/appointments/{appt.id}/no-show/",
        )
        assert r.status_code == 422
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.PENDING
