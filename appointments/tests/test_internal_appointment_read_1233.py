"""DRF-1233 — the canonical `version`, and who may read it.

This endpoint exists because bot-platform could not offer a reschedule at
all: Ayla requires `expected_version` and the bot had no way to learn it.
Measured on the pilot 2026-08-21 — 2 of 23 mirrored bookings carried a
version, and the single future confirmed booking carried none.

Two tests carry the weight:

* :meth:`TestVisibility.test_an_unrelated_actor_gets_404_not_403` — the
  internal tree must not confirm which appointment ids exist;
* :meth:`TestShape.test_exactly_four_fields` — this is not a
  booking-detail endpoint, and each extra field would be a customer's
  data crossing a service boundary for no reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

VALID_TOKEN = "test-ayla-internal-token-1233"  # pragma: allowlist secret
CUSTOMER_EXTERNAL_ID = "bot:max:1233customer"
ADMIN_EXTERNAL_ID = "bot:max:1233admin"
STRANGER_EXTERNAL_ID = "bot:max:1233stranger"


def _url(booking_id) -> str:
    return f"/api/v1/internal/appointments/{booking_id}/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="i1233-t", name="Read Tenant")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(slug="i1233-t-b", name="Other Read Tenant")


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=CUSTOMER_EXTERNAL_ID, password="x", role="client",
        phone="+79991233000", is_proxy=True,
    )


@pytest.fixture
def salon_admin(db, tenant):
    u = User.objects.create_user(
        username=ADMIN_EXTERNAL_ID, password="x", role="admin",
        phone="+79991233001",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


@pytest.fixture
def stranger(db):
    return User.objects.create_user(
        username=STRANGER_EXTERNAL_ID, password="x", role="client",
        phone="+79991233002",
    )


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="i1233_spec", password="x", role="specialist",
        phone="+79991233003",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Read Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def service(specialist, db):
    category = ServiceCategory.objects.create(name="I1233 Cat", slug="i1233-cat")
    return Service.objects.create(
        specialist=specialist, category=category, name="Read Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def booking(tenant, customer, specialist, service):
    starts_at = datetime.now(tz=timezone.utc) + timedelta(days=1)
    return Appointment.objects.create(
        tenant=tenant,
        client=customer,
        specialist=specialist,
        service=service,
        salon_service=None,
        start_datetime=starts_at,
        end_datetime=starts_at + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED,
        version=1,
        price=Decimal("1500.00"),
    )


def _api(
    *,
    bearer: str | None = VALID_TOKEN,
    external_user_id: str | None = CUSTOMER_EXTERNAL_ID,
) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


@pytest.mark.django_db
class TestAuth:
    """Through the front door — a real Bearer, never force_authenticate.

    DRF-1231 was invisible to every existing test precisely because they
    all skipped this layer.
    """

    def test_no_bearer_is_refused(self, booking):
        assert _api(bearer=None).get(_url(booking.id)).status_code in (401, 403)

    def test_a_wrong_bearer_is_refused(self, booking):
        assert _api(bearer="nope").get(_url(booking.id)).status_code in (401, 403)

    def test_no_actor_header_is_refused(self, booking):
        """The token alone must not be enough to read somebody's booking."""
        resp = _api(external_user_id=None).get(_url(booking.id))
        assert resp.status_code in (401, 403)

    def test_the_app_type_header_is_not_required(self, booking, customer):
        """`/api/v1/internal/` is already exempt, and the bot is neither
        client nor pro. No X-App-Type is sent anywhere in this module."""
        assert _api().get(_url(booking.id)).status_code == 200


@pytest.mark.django_db
class TestVisibility:
    def test_the_customer_sees_their_own_booking(self, booking, customer):
        assert _api().get(_url(booking.id)).status_code == 200

    def test_an_admin_of_that_salon_sees_it(self, booking, salon_admin):
        """The salon console's case: the actor is the administrator, not
        the customer, so the sibling views' `client=request.user` rule
        would have made this endpoint useless for the thing it is for."""
        resp = _api(external_user_id=ADMIN_EXTERNAL_ID).get(_url(booking.id))
        assert resp.status_code == 200

    def test_an_admin_of_another_salon_does_not(self, booking, other_tenant, db):
        outsider = User.objects.create_user(
            username="bot:max:1233outsider", password="x", role="admin",
            phone="+79991233004",
        )
        TenantUserRelationship.objects.create(
            user=outsider, tenant=other_tenant,
            role=TenantUserRelationship.Role.ADMIN, is_active=True,
        )
        resp = _api(external_user_id="bot:max:1233outsider").get(_url(booking.id))
        assert resp.status_code == 404

    def test_a_customer_relationship_is_not_enough(self, booking, tenant, db):
        """`customer` and `admin` are different roles, and only one of
        them may look at other people's bookings."""
        other_client = User.objects.create_user(
            username="bot:max:1233othercli", password="x", role="client",
            phone="+79991233005",
        )
        TenantUserRelationship.objects.create(
            user=other_client, tenant=tenant,
            role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
        )
        resp = _api(external_user_id="bot:max:1233othercli").get(_url(booking.id))
        assert resp.status_code == 404

    def test_a_deactivated_admin_relationship_does_not_count(
        self, booking, tenant, db
    ):
        former = User.objects.create_user(
            username="bot:max:1233former", password="x", role="admin",
            phone="+79991233006",
        )
        TenantUserRelationship.objects.create(
            user=former, tenant=tenant,
            role=TenantUserRelationship.Role.ADMIN, is_active=False,
        )
        resp = _api(external_user_id="bot:max:1233former").get(_url(booking.id))
        assert resp.status_code == 404

    def test_an_unrelated_actor_gets_404_not_403(self, booking, stranger):
        """403 would confirm the booking exists. 404 says nothing."""
        resp = _api(external_user_id=STRANGER_EXTERNAL_ID).get(_url(booking.id))
        assert resp.status_code == 404

    def test_an_unknown_id_looks_identical_to_a_hidden_one(self, booking, stranger):
        hidden = _api(external_user_id=STRANGER_EXTERNAL_ID).get(_url(booking.id))
        missing = _api(external_user_id=STRANGER_EXTERNAL_ID).get(_url(uuid4()))
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()


@pytest.mark.django_db
class TestShape:
    def test_returns_the_canonical_version(self, booking, customer):
        booking.version = 7
        booking.save(update_fields=["version"])

        data = _api().get(_url(booking.id)).json()["data"]

        assert data["version"] == 7
        assert data["id"] == str(booking.id)
        assert data["status"] == booking.status

    def test_exactly_four_fields(self, booking, customer):
        """Not a booking-detail endpoint, and must not become one.

        Every extra field is a customer's data crossing a service
        boundary for no reason — the console already has the rest from
        its own mirror.
        """
        data = _api().get(_url(booking.id)).json()["data"]
        assert set(data) == {"id", "version", "status", "start_datetime"}

    def test_no_customer_identity_leaks(self, booking, customer):
        import json as _json

        body = _json.dumps(_api().get(_url(booking.id)).json(), ensure_ascii=False)

        assert customer.phone not in body
        assert str(customer.id) not in body

    def test_start_datetime_is_an_iso_timestamp(self, booking, customer):
        data = _api().get(_url(booking.id)).json()["data"]
        assert data["start_datetime"].startswith(
            booking.start_datetime.isoformat()[:16]
        )

    def test_the_wire_name_matches_the_sibling_views(self, booking, customer):
        """`start_datetime` on the wire, not bot-platform's `start_at`.

        The three internal write views in this file already take
        `start_datetime` and translate to `start_at` inside their DTOs,
        and the bot's client already sends the wire name when it calls
        them. A read that answered `start_at` would be the odd one out
        among exactly the endpoints its only caller already speaks to.
        """
        data = _api().get(_url(booking.id)).json()["data"]
        assert "start_at" not in data


@pytest.mark.django_db
class TestItIsAReadAndNothingElse:
    def test_the_booking_is_untouched(self, booking, customer):
        before = (booking.version, booking.status, booking.start_datetime)

        _api().get(_url(booking.id))

        booking.refresh_from_db()
        assert (booking.version, booking.status, booking.start_datetime) == before

    def test_the_bare_id_route_did_not_swallow_the_write_routes(self, booking):
        """A new `<uuid>/` pattern next to `<uuid>/cancel/` is exactly the
        shape that silently captures its siblings when someone later
        changes a converter. Asserted by resolution, not by a live call,
        so the check stays about routing.
        """
        from django.urls import resolve

        assert resolve(_url(booking.id)).url_name == "internal-appointment-read"
        assert resolve(
            f"/api/v1/internal/appointments/{booking.id}/cancel/"
        ).url_name == "internal-booking-cancel"
        assert resolve(
            f"/api/v1/internal/appointments/{booking.id}/reschedule/"
        ).url_name == "internal-booking-reschedule"
