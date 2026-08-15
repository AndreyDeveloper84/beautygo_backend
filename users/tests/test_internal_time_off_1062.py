"""POST /api/v1/internal/specialists/{id}/time-off/ — DRF-1062.

The route exists so the bot's Mini App can approve a master's day-off
request into Ayla, now that the customer picker reads slots from Ayla. An
approval written into the bot's own store would leave the day on sale
while telling the administrator it worked.

The security shape is the point of most of these tests: ``tenant_id``
arrives in the body, so it is a claim to be checked, not a credential. A
specialist in another tenant must be indistinguishable from one that does
not exist — 404, never 403, or the surface confirms which UUIDs are real
to whoever guessed one.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment, SpecialistTimeOff
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

TOKEN = "internal-token-1062"


def _url(specialist_id) -> str:
    return f"/api/v1/internal/specialists/{specialist_id}/time-off/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def bearer() -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    return c


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="int-to-a", name="Салон А")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="int-to-b", name="Салон Б")


def _master(username, tenant, phone):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = username
    profile.timezone = "Europe/Moscow"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def master(db, salon):
    return _master("int_to_master", salon, "+79993062001")


@pytest.fixture
def window():
    start = (timezone.now() + timedelta(days=4)).replace(
        hour=9, minute=0, second=0, microsecond=0,
    )
    return start, start + timedelta(hours=8)


def _body(tenant, window, reason="выходной по заявке"):
    start, end = window
    return {
        "tenant_id": str(tenant.id),
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "reason": reason,
    }


class TestHappyPath:
    def test_blocks_the_time(self, bearer, master, salon, window):
        resp = bearer.post(_url(master.id), _body(salon, window), format="json")

        assert resp.status_code == 201
        assert SpecialistTimeOff.objects.filter(specialist=master).count() == 1

    def test_response_carries_no_personal_data(self, bearer, master, salon, window):
        """Adjacent to DRF-1036: the route reports the block it wrote and
        nothing about the people it affects."""
        resp = bearer.post(_url(master.id), _body(salon, window), format="json")

        assert set(resp.data["data"]) == {
            "id", "specialist_id", "start_at", "end_at", "reason",
        }

    def test_end_must_be_after_start(self, bearer, master, salon, window):
        start, end = window
        body = _body(salon, (end, start))

        resp = bearer.post(_url(master.id), body, format="json")

        assert resp.status_code == 400


class TestTenantBoundary:
    def test_foreign_tenant_id_is_404_not_403(
        self, bearer, master, other_salon, window,
    ):
        """The mandatory one. A valid bearer plus a real specialist id plus
        somebody else's tenant must be indistinguishable from a specialist
        that does not exist — a 403 would confirm the UUID is real."""
        resp = bearer.post(
            _url(master.id), _body(other_salon, window), format="json",
        )

        assert resp.status_code == 404
        assert not SpecialistTimeOff.objects.exists()

    def test_unknown_specialist_is_the_same_404(self, bearer, salon, window):
        import uuid

        resp = bearer.post(
            _url(uuid.uuid4()), _body(salon, window), format="json",
        )

        assert resp.status_code == 404

    def test_specialist_without_a_tenant_is_not_reachable(
        self, bearer, master, salon, window,
    ):
        master.tenant = None
        master.save(update_fields=["tenant"])

        resp = bearer.post(_url(master.id), _body(salon, window), format="json")

        assert resp.status_code == 404

    def test_tenant_id_is_required(self, bearer, master, window):
        start, end = window

        resp = bearer.post(
            _url(master.id),
            {"start_at": start.isoformat(), "end_at": end.isoformat()},
            format="json",
        )

        assert resp.status_code == 400


class TestAuth:
    def test_no_bearer_is_refused(self, master, salon, window):
        resp = APIClient().post(_url(master.id), _body(salon, window), format="json")

        assert resp.status_code in (401, 403)
        assert not SpecialistTimeOff.objects.exists()

    def test_wrong_bearer_is_refused(self, master, salon, window):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer nope")

        resp = c.post(_url(master.id), _body(salon, window), format="json")

        assert resp.status_code in (401, 403)


class TestLiveBookingsStillProtected:
    def test_active_appointment_blocks_with_409(
        self, bearer, master, salon, window, db,
    ):
        """Same rule the human surfaces enforce. Settling those bookings
        needs a named actor, not a shared service token, so that flow is
        deliberately not reachable from here."""
        start, _ = window
        category = ServiceCategory.objects.create(name="И1062", slug="i1062")
        service = Service.objects.create(
            specialist=master, category=category, name="Услуга",
            price="1000.00", duration_minutes=60, is_active=True,
        )
        client_user = User.objects.create_user(
            username="int_to_client", password="x", phone="+79993062009",
        )
        at = start + timedelta(hours=2)
        Appointment.objects.create(
            client=client_user, specialist=master, service=service,
            start_datetime=at, end_datetime=at + timedelta(minutes=60),
            price=service.price, status=Appointment.Status.CONFIRMED,
            snapshot_service_name=service.name, snapshot_price=service.price,
            snapshot_duration_minutes=60,
        )

        resp = bearer.post(_url(master.id), _body(salon, window), format="json")

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"
        assert not SpecialistTimeOff.objects.exists()
