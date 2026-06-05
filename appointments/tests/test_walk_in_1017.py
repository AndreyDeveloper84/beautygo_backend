"""Provider walk-in booking — POST /api/v1/appointments/walk-in/ (#1017).

A master records an off-platform walk-in into their own diary so the
slot shows busy and the bot's mirror does not double-book it. Pins:
- specialist creates a walk-in → 201, status CONFIRMED, NO Payment row,
  proxy client (is_proxy), name stored;
- the slot is then held — a second booking on it conflicts (409/400);
- booking.created + booking.confirmed events are emitted;
- a client (non-specialist) cannot create a walk-in → 403;
- phone dedupe — same phone reuses the proxy user, no phone → fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, OutboxEvent
from payments.models import Payment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


WALK_IN_URL = "/api/v1/appointments/walk-in/"


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="wi-t", name="Walk-in Tenant")


@pytest.fixture
def specialist_user(db, tenant):
    u = User.objects.create_user(
        username="wi_spec", password="x", role="specialist",
        phone="+79991017001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Walk-in Master"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return u


@pytest.fixture
def specialist(specialist_user):
    return specialist_user.specialist_profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="WI Cat", slug="wi-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="Walk-in Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="wi_client", password="x", role="client",
        phone="+79991017050",
    )


def _pro(user) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    c.force_authenticate(user=user)
    return c


def _body(service, *, name="Мария", phone="", hours=3) -> dict:
    return {
        "service_id": str(service.id),
        "start_datetime": _future_iso(hours),
        "client_name": name,
        "client_phone": phone,
    }


@pytest.mark.django_db
class TestWalkInCreate:
    def test_specialist_creates_confirmed_walkin_no_payment(
        self, specialist_user, specialist, service,
    ):
        r = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Мария"), format="json",
        )
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get()
        assert appt.specialist_id == specialist.id
        assert appt.status == Appointment.Status.CONFIRMED
        # No online Payment row for an off-platform walk-in.
        assert Payment.objects.filter(appointment=appt).count() == 0
        # Client is a proxy stub carrying the walk-in name.
        assert appt.client.is_proxy is True
        assert appt.client.first_name == "Мария"
        assert "Мария" in appt.notes

    def test_walkin_holds_the_slot(self, specialist_user, specialist, service):
        body = _body(service, name="Мария", hours=4)
        r1 = _pro(specialist_user).post(WALK_IN_URL, body, format="json")
        assert r1.status_code == 201
        # A second walk-in on the same slot conflicts (slot now busy).
        r2 = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Олег", hours=4), format="json",
        )
        assert r2.status_code in (400, 409)
        assert Appointment.objects.count() == 1

    def test_walkin_emits_created_and_confirmed_events(
        self, specialist_user, specialist, service,
    ):
        r = _pro(specialist_user).post(
            WALK_IN_URL, _body(service), format="json",
        )
        assert r.status_code == 201
        # Isolated test DB — the walk-in is the only thing emitting here.
        topics = set(
            OutboxEvent.objects.values_list("topic", flat=True)
        )
        assert OutboxEvent.Topic.BOOKING_CREATED in topics
        assert OutboxEvent.Topic.BOOKING_CONFIRMED in topics

    def test_client_cannot_create_walkin(
        self, client_user, service,
    ):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=client_user)
        r = c.post(WALK_IN_URL, _body(service), format="json")
        assert r.status_code == 403
        assert Appointment.objects.count() == 0


@pytest.mark.django_db
class TestWalkInClientResolution:
    def test_same_phone_reuses_user(
        self, specialist_user, specialist, service,
    ):
        phone = "+79991017099"
        r1 = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Аня", phone=phone, hours=3),
            format="json",
        )
        r2 = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Аня", phone=phone, hours=6),
            format="json",
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        ids = {
            Appointment.objects.get(id=r1.data["data"]["id"]).client_id,
            Appointment.objects.get(id=r2.data["data"]["id"]).client_id,
        }
        assert len(ids) == 1  # same phone → one proxy user
        assert User.objects.filter(phone=phone).count() == 1

    def test_no_phone_creates_fresh_proxy_each_time(
        self, specialist_user, specialist, service,
    ):
        r1 = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Гость", hours=3), format="json",
        )
        r2 = _pro(specialist_user).post(
            WALK_IN_URL, _body(service, name="Гость", hours=6), format="json",
        )
        assert r1.status_code == 201 and r2.status_code == 201
        c1 = Appointment.objects.get(id=r1.data["data"]["id"]).client_id
        c2 = Appointment.objects.get(id=r2.data["data"]["id"]).client_id
        assert c1 != c2  # no phone → distinct stubs

    def test_existing_registered_phone_is_linked(
        self, specialist_user, specialist, service, client_user,
    ):
        """A walk-in whose phone matches a real registered customer
        attaches to that account (keeps one identity + history)."""
        r = _pro(specialist_user).post(
            WALK_IN_URL,
            _body(service, name="Reg", phone=client_user.phone, hours=5),
            format="json",
        )
        assert r.status_code == 201
        appt = Appointment.objects.get(id=r.data["data"]["id"])
        assert appt.client_id == client_user.id
