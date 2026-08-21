"""DRF-1234 — the salon closes a visit.

Everything hanging off completion — commission, payment capture, the
review request, RFM — runs off `booking.completed`. Before DRF-1064 no
booking in this system had ever reached `completed`, because the only
people who could close one could not log in. That endpoint fixed who may
close; this one fixes where from, since the mobile path is unreachable
for the bot.

The load-bearing tests:

* :meth:`TestItIsTheSameClosure.test_the_completed_event_is_emitted` —
  a wrapper that closed the row without emitting would leave every
  downstream consumer believing the visit never happened;
* :meth:`TestConcurrency.test_a_stale_version_is_refused` — the guard
  that stops a caller closing a booking that moved under them;
* :meth:`TestScope.test_a_booking_of_another_salon_is_not_found`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "salon-complete-token-under-test"  # pragma: allowlist secret
ADMIN_EXTERNAL_ID = "bot:max:1234admin"


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="c1234-t", name="Complete Salon")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="c1234-t-b", name="Other Complete Salon")


@pytest.fixture
def admin_user(db, salon):
    u = User.objects.create_user(
        username=ADMIN_EXTERNAL_ID, password="x", role="admin",
        phone="+79991234000",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


@pytest.fixture
def customer(db, salon):
    u = User.objects.create_user(
        username="bot:max:1234client", password="x", role="client",
        phone="+79991234001", first_name="Мария",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
    )
    return u


@pytest.fixture
def master(db, salon):
    u = User.objects.create_user(
        username="c1234_master", password="x", role="specialist",
        phone="+79991234002",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Ольга"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="C1234 Cat", slug="c1234-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Массаж",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _booking(salon, customer, master, service, *, status=None, version=1):
    started = datetime.now(tz=dt_timezone.utc) - timedelta(hours=2)
    return Appointment.objects.create(
        tenant=salon,
        client=customer,
        specialist=master,
        service=service,
        salon_service=None,
        start_datetime=started,
        end_datetime=started + timedelta(hours=1),
        status=status or Appointment.Status.CONFIRMED,
        version=version,
        price=Decimal("2000.00"),
    )


@pytest.fixture
def booking(salon, customer, master, service):
    return _booking(salon, customer, master, service)


def _api(user, *, tenant_slug=None, token=SERVICE_TOKEN) -> APIClient:
    """Real Bearer, never force_authenticate — see DRF-1231.

    The salon surface refused every live request with 401 for as long as
    it existed, and no test noticed, because they all skipped this layer.
    """
    c = APIClient()
    if tenant_slug:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    if token:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if user is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    return c


def _url(appointment_id) -> str:
    return f"/api/v1/tenants/me/appointments/{appointment_id}/complete/"


def _complete(api, booking, *, version=None):
    return api.post(
        _url(booking.id),
        {"expected_version": booking.version if version is None else version},
        format="json",
    )


@pytest.mark.django_db(transaction=True)
class TestItIsTheSameClosure:
    def test_the_visit_is_completed(self, salon, admin_user, booking):
        resp = _complete(_api(admin_user, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.COMPLETED

    def test_the_completed_event_is_emitted(self, salon, admin_user, booking):
        """Commission, capture, the review request and RFM all hang off
        this event. A closure without it is a visit that, to every
        consumer, never happened."""
        _complete(_api(admin_user, tenant_slug=salon.slug), booking)

        topics = set(OutboxEvent.objects.values_list("topic", flat=True))
        assert OutboxEvent.Topic.BOOKING_COMPLETED in topics

    def test_the_closure_is_attributed_to_the_salon(
        self, salon, admin_user, booking
    ):
        """Not to the customer, and not to the master who did not press
        the button. The whole point of the salon surface."""
        _complete(_api(admin_user, tenant_slug=salon.slug), booking)

        booking.refresh_from_db()
        assert booking.completed_by == "salon"

    def test_version_is_not_bumped_by_closing(self, salon, admin_user, booking):
        """`version` counts reschedules. If closure bumped it, a client
        holding a version read a second ago would be told it is stale."""
        before = booking.version

        _complete(_api(admin_user, tenant_slug=salon.slug), booking)

        booking.refresh_from_db()
        assert booking.version == before


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_a_stale_version_is_refused(self, salon, admin_user, booking):
        resp = _complete(_api(admin_user, tenant_slug=salon.slug), booking, version=99)

        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "STALE_VERSION"
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_a_missing_version_is_refused(self, salon, admin_user, booking):
        """Required on this surface, unlike mobile: the console reads the
        booking and its version immediately before offering the button,
        so an omitted one is a caller bug, not a legacy build."""
        resp = _api(admin_user, tenant_slug=salon.slug).post(
            _url(booking.id), {}, format="json",
        )

        assert resp.status_code == 400
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_closing_twice_is_refused_the_second_time(
        self, salon, admin_user, booking
    ):
        api = _api(admin_user, tenant_slug=salon.slug)

        assert _complete(api, booking).status_code == 200
        booking.refresh_from_db()
        second = _complete(api, booking)

        assert second.status_code == 422
        # Exactly one event — a second would double-close downstream.
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        ).count() == 1


@pytest.mark.django_db(transaction=True)
class TestStatesThatCannotClose:
    def test_a_cancelled_visit_cannot_be_completed(
        self, salon, admin_user, customer, master, service
    ):
        cancelled = _booking(
            salon, customer, master, service,
            status=Appointment.Status.CANCELLED,
        )

        resp = _complete(_api(admin_user, tenant_slug=salon.slug), cancelled)

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "INVALID_STATUS"

    def test_a_pending_visit_cannot_be_completed(
        self, salon, admin_user, customer, master, service
    ):
        pending = _booking(
            salon, customer, master, service,
            status=Appointment.Status.PENDING,
        )

        assert _complete(
            _api(admin_user, tenant_slug=salon.slug), pending,
        ).status_code == 422


@pytest.mark.django_db(transaction=True)
class TestScope:
    def test_a_booking_of_another_salon_is_not_found(
        self, salon, other_salon, admin_user, customer, master, service
    ):
        stranger = _booking(other_salon, customer, master, service)

        resp = _complete(_api(admin_user, tenant_slug=salon.slug), stranger)

        assert resp.status_code == 404
        stranger.refresh_from_db()
        assert stranger.status == Appointment.Status.CONFIRMED

    def test_an_unknown_booking_is_not_found(self, salon, admin_user):
        resp = _api(admin_user, tenant_slug=salon.slug).post(
            _url(uuid4()), {"expected_version": 1}, format="json",
        )
        assert resp.status_code == 404

    def test_a_customer_may_not_close_their_own_visit(
        self, salon, customer, booking
    ):
        """A relationship with the salon is not operational standing."""
        resp = _complete(_api(customer, tenant_slug=salon.slug), booking)

        assert resp.status_code == 403
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_without_a_tenant_header_nothing_closes(self, admin_user, booking):
        """IsTenantAdmin authorises against request.tenant, which only
        exists because TenantContextMiddleware still reads X-Tenant on
        this path — it is exempt from X-App-Type, not from X-Tenant."""
        resp = _complete(_api(admin_user), booking)

        assert resp.status_code == 403
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
class TestAuth:
    def test_no_bearer_closes_nothing(self, salon, admin_user, booking):
        resp = _complete(
            _api(admin_user, tenant_slug=salon.slug, token=None), booking,
        )

        assert resp.status_code in (401, 403)
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_a_wrong_bearer_closes_nothing(self, salon, admin_user, booking):
        resp = _complete(
            _api(admin_user, tenant_slug=salon.slug, token="nope"), booking,
        )

        assert resp.status_code in (401, 403)

    def test_the_token_alone_names_nobody(self, salon, booking):
        resp = _complete(_api(None, tenant_slug=salon.slug), booking)

        assert resp.status_code in (401, 403)

    def test_no_app_type_header_is_needed(self, salon, admin_user, booking):
        """Nothing in this module sends X-App-Type, and the happy path
        above returns 200 — which is the assertion."""
        assert _complete(
            _api(admin_user, tenant_slug=salon.slug), booking,
        ).status_code == 200


@pytest.mark.django_db(transaction=True)
class TestNoShowIsNotHere:
    def test_the_no_show_route_does_not_exist_on_this_surface(
        self, salon, admin_user, booking
    ):
        """Deliberate, not forgotten.

        Its mobile implementation shapes two outbox events inline, and a
        copy would be two paths obliged to agree forever. Extracting a
        sibling of `close_booking` first is its own task — this test
        fails the day somebody adds the route without doing that, which
        is the moment to re-read the reasoning.
        """
        resp = _api(admin_user, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{booking.id}/no_show/",
            {}, format="json",
        )
        assert resp.status_code == 404
