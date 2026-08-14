"""DRF-1049 — repeat-intent must honour the AMD-019 service XOR.

``Appointment`` carries EXACTLY ONE typed service reference: the
marketplace ``service`` OR the salon-catalog ``salon_service`` (CHECK
``appointment_exactly_one_service_source``). The records list/detail
readers resolve that XOR; ``MeBookingRepeatIntentView`` used to read
``appointment.service_id`` directly and therefore returned the literal
string ``"None"`` with HTTP 200 for every salon booking — which is
100% of the bookings the bot creates on the pilot.

Covered here:
- salon booking (``salon_service`` set, ``service`` NULL) — the pilot case;
- legacy marketplace booking (``service`` set) — no regression;
- neither reference resolvable — explicit 4xx, never ``"None"``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from django.db import connection
from rest_framework.test import APIClient

from appointments.models import Appointment
from appointments.records_api import _resolve_service_id
from services.models import SalonService, Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-1049"
EXTERNAL_USER_ID = "bot:ri1049"


def _repeat_url(booking_id) -> str:
    return f"/api/v1/internal/me/bookings/{booking_id}/repeat-intent/"


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79996104901", is_proxy=True,
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="ri1049-t", name="RI1049 Tenant")


@pytest.fixture
def specialist(db, tenant):
    user = User.objects.create_user(
        username="ri1049_spec", password="x", role="specialist",
        phone="+79996104902",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = "RI1049 Spec"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="RI1049 Cat", slug="ri1049-cat")


@pytest.fixture
def salon_service(tenant, category):
    return SalonService.objects.create(
        tenant=tenant, category=category, name="RI1049 Salon",
        duration_minutes=60,
    )


@pytest.fixture
def marketplace_service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="RI1049 Mkt",
        price=Decimal("1500.00"), duration_minutes=45, is_active=True,
    )


def _make_appointment(customer, specialist, **service_ref) -> Appointment:
    start = datetime.now(tz=timezone.utc) + timedelta(hours=3)
    return Appointment.objects.create(
        client=customer,
        specialist=specialist,
        tenant=specialist.tenant,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED,
        price=Decimal("2000.00"),
        snapshot_service_name="RI1049 Snapshot",
        snapshot_duration_minutes=60,
        snapshot_price=Decimal("2000.00"),
        **service_ref,
    )


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRepeatIntentServiceXor:
    def test_salon_booking_returns_salon_service_id(
        self, customer, specialist, salon_service,
    ):
        """The pilot case: ``service_id IS NULL``, salon reference set.

        Pre-fix this returned the string "None" with HTTP 200.
        """
        appt = _make_appointment(
            customer, specialist, salon_service=salon_service,
        )
        assert appt.service_id is None

        r = _api().post(_repeat_url(appt.id))

        assert r.status_code == 200, r.data
        body = r.json()["data"]
        assert body["service_id"] == str(salon_service.id)
        assert body["service_id"] != "None"
        assert body["specialist_id"] == str(specialist.id)
        assert Decimal(body["last_price"]) == appt.price
        assert body["suggested_slots"] == []

    def test_marketplace_booking_still_returns_service_id(
        self, customer, specialist, marketplace_service,
    ):
        """Legacy shape — marketplace ``service`` set, salon NULL."""
        appt = _make_appointment(
            customer, specialist, service=marketplace_service,
        )
        assert appt.salon_service_id is None

        r = _api().post(_repeat_url(appt.id))

        assert r.status_code == 200, r.data
        body = r.json()["data"]
        assert body["service_id"] == str(marketplace_service.id)
        assert body["specialist_id"] == str(specialist.id)

    def test_unresolvable_service_is_an_error_not_a_none_string(
        self, customer, specialist, salon_service,
    ):
        """Neither reference set → explicit 422, no "None" payload.

        The XOR CHECK makes this unreachable for rows created today, so
        the constraint is dropped inside the test transaction to build
        the legacy/corrupt row. pytest-django rolls the DDL back with
        the rest of the transaction.
        """
        appt = _make_appointment(
            customer, specialist, salon_service=salon_service,
        )
        with connection.cursor() as cur:
            # Flush deferred FK triggers from the inserts above —
            # Postgres refuses ALTER TABLE while they are pending.
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cur.execute(
                "ALTER TABLE appointments_appointment "
                "DROP CONSTRAINT appointment_exactly_one_service_source",
            )
        Appointment.objects.filter(pk=appt.pk).update(salon_service=None)
        appt.refresh_from_db()
        assert appt.service_id is None and appt.salon_service_id is None

        r = _api().post(_repeat_url(appt.id))

        assert r.status_code == 422, r.data
        assert r.json()["error"]["code"] == "SERVICE_NOT_FOUND"
        assert "None" not in str(r.json().get("data", ""))


# ---------------------------------------------------------------------------
# Resolver unit
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, service_id=None, salon_service_id=None):
        self.service_id = service_id
        self.salon_service_id = salon_service_id


class TestResolveServiceId:
    def test_marketplace_side(self):
        assert _resolve_service_id(_Stub(service_id="svc-1")) == "svc-1"

    def test_salon_side(self):
        assert _resolve_service_id(_Stub(salon_service_id="salon-1")) == "salon-1"

    def test_neither_side_returns_none_not_the_string(self):
        assert _resolve_service_id(_Stub()) is None
