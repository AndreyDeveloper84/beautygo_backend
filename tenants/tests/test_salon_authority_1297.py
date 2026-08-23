"""DRF-1297 В-2 — who may act on a salon booking, pinned.

The second audit reported create / reschedule / cancel as missing the
per-action authority check that ``complete`` and ``no_show`` have. Read
against ``dev`` that is not what the code says: all three already refuse
exactly what ``appointments.authz.resolve_booking_operator`` would
refuse, by composing ``IsTenantAdmin`` (an active admin grant in
``request.tenant``) with a row lookup filtered to that same tenant.

What was genuinely missing is this file. Not one test asserted any of
it, so the property held by construction and by nothing else: relax the
permission list, or drop the ``tenant=`` from ``_get_booking``, and the
suite stayed green.

The owner's list is the structure here, one class per line of it:

* a permitted actor succeeds;
* an actor without the grant is refused;
* ``User.role == "admin"`` **without** a tenant grant is refused — the
  salon's authority lives in the relationship row and nowhere else;
* being able to *see* a salon is not being able to *act* in it.

Every request uses the bot's real credential set, because that is who
calls this surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "salon-authority-token-under-test"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="a1297-a", name="Authority Salon A")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="a1297-b", name="Authority Salon B")


def _user(username, *, role="client", phone):
    return User.objects.create_user(
        username=username, password="x", role=role, phone=phone,
    )


def _grant(user, tenant, role):
    return TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=role, is_active=True,
    )


@pytest.fixture
def admin_a(salon):
    user = _user("bot:max:a1297a", phone="+79995401001")
    _grant(user, salon, TenantUserRelationship.Role.ADMIN)
    return user


@pytest.fixture
def admin_b(other_salon):
    """Administers a different salon. The impersonation attempt."""
    user = _user("bot:max:a1297b", phone="+79995401002")
    _grant(user, other_salon, TenantUserRelationship.Role.ADMIN)
    return user


@pytest.fixture
def customer_of_a(salon):
    """A customer relationship with the salon — a relationship, but not
    an authority. The row's ``role`` is the whole difference."""
    user = _user("bot:max:a1297cust", phone="+79995401003")
    _grant(user, salon, TenantUserRelationship.Role.CUSTOMER)
    return user


@pytest.fixture
def staff_of_a(salon):
    """``Role.STAFF`` is read by no authorisation code anywhere. Pinned
    so that stays a decision rather than an oversight."""
    user = _user("bot:max:a1297staff", phone="+79995401004")
    _grant(user, salon, TenantUserRelationship.Role.STAFF)
    return user


@pytest.fixture
def role_admin_without_grant(db):
    """``User.role == "admin"`` and no relationship row anywhere.

    ``IsTenantAdmin`` never reads ``User.role``. This is the account
    shape ``provision_salon_admin`` creates from scratch, and the reason
    the pilot administrator has to be a client account promoted by a
    grant instead.
    """
    return _user("bot:max:a1297roleadmin", role="admin", phone="+79995401005")


@pytest.fixture
def revoked_admin(salon):
    """The grant exists but ``is_active=False`` — revocation must bite."""
    user = _user("bot:max:a1297revoked", phone="+79995401006")
    TenantUserRelationship.objects.create(
        user=user, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=False,
    )
    return user


def _make_master(tenant, *, username, phone, name):
    user = _user(username, role="specialist", phone=phone)
    user.tenant = tenant
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.tenant = tenant
    profile.save()
    return profile


@pytest.fixture
def master(salon):
    return _make_master(
        salon, username="a1297_m", phone="+79995401007", name="Ольга",
    )


@pytest.fixture
def other_master(other_salon):
    return _make_master(
        other_salon, username="a1297_m_b", phone="+79995401008", name="Инна",
    )


@pytest.fixture
def client_user(salon):
    user = _user("bot:max:a1297client", phone="+79995401009")
    _grant(user, salon, TenantUserRelationship.Role.CUSTOMER)
    return user


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="A1297", slug="a1297-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _make_booking(tenant, client, master, service):
    start = datetime.now(tz=dt_timezone.utc) + timedelta(days=3)
    start = start.replace(minute=0, second=0, microsecond=0)
    return Appointment.objects.create(
        tenant=tenant,
        client=client,
        specialist=master,
        service=service,
        salon_service=None,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED,
        version=1,
        price=Decimal("2000.00"),
    )


@pytest.fixture
def booking(salon, client_user, master, service):
    return _make_booking(salon, client_user, master, service)


@pytest.fixture
def booking_of_b(other_salon, client_user, other_master, service):
    return _make_booking(other_salon, client_user, other_master, service)


def _api(user, *, tenant_slug) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {SERVICE_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    client.defaults["HTTP_X_IDEMPOTENCY_KEY"] = f"a1297-{user.username}"
    if tenant_slug:
        client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _reschedule(api, booking, *, version=None):
    new_start = booking.start_datetime + timedelta(hours=2)
    return api.post(
        f"/api/v1/tenants/me/appointments/{booking.id}/reschedule/",
        {
            "new_start_datetime": new_start.isoformat(),
            "expected_version": booking.version if version is None else version,
        },
        format="json",
    )


def _cancel(api, booking):
    return api.post(
        f"/api/v1/tenants/me/appointments/{booking.id}/cancel/",
        {"reason": "тест"},
        format="json",
    )


def _create(api, master, service, client_user):
    start = datetime.now(tz=dt_timezone.utc) + timedelta(days=5)
    return api.post(
        "/api/v1/tenants/me/appointments/",
        {
            "specialist_id": str(master.id),
            "service_id": str(service.id),
            "start_datetime": start.replace(
                minute=0, second=0, microsecond=0,
            ).isoformat(),
            "client_id": str(client_user.id),
        },
        format="json",
    )


@pytest.mark.django_db(transaction=True)
class TestThePermittedActorSucceeds:
    """The other half of every negative test below.

    Without these, a permission list that refused everyone would pass the
    whole file.
    """

    def test_the_salon_administrator_reschedules(self, salon, admin_a, booking):
        resp = _reschedule(_api(admin_a, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data

    def test_the_salon_administrator_cancels(self, salon, admin_a, booking):
        resp = _cancel(_api(admin_a, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CANCELLED


@pytest.mark.django_db(transaction=True)
class TestAnActorWithoutTheGrantIsRefused:
    @pytest.mark.parametrize(
        "actor_fixture",
        [
            "customer_of_a",
            "staff_of_a",
            "role_admin_without_grant",
            "revoked_admin",
        ],
    )
    @pytest.mark.parametrize(
        "operation", ["reschedule", "cancel", "create"],
    )
    def test_it_is_refused(
        self, request, salon, master, service, client_user, booking,
        actor_fixture, operation,
    ):
        actor = request.getfixturevalue(actor_fixture)
        api = _api(actor, tenant_slug=salon.slug)

        if operation == "reschedule":
            resp = _reschedule(api, booking)
        elif operation == "cancel":
            resp = _cancel(api, booking)
        else:
            resp = _create(api, master, service, client_user)

        assert resp.status_code == 403, resp.data

    def test_nothing_changed_after_a_refusal(
        self, salon, customer_of_a, booking
    ):
        before = booking.start_datetime

        _reschedule(_api(customer_of_a, tenant_slug=salon.slug), booking)

        booking.refresh_from_db()
        assert booking.start_datetime == before
        assert booking.status == Appointment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
class TestAuthorityIsPerSalonNotGlobal:
    """Administering *a* salon is not administering *this* one."""

    def test_an_administrator_of_another_salon_cannot_act_here(
        self, salon, admin_b, booking
    ):
        """Names salon A in X-Tenant while holding a grant only in B."""
        resp = _cancel(_api(admin_b, tenant_slug=salon.slug), booking)

        assert resp.status_code == 403, resp.data

    def test_an_administrator_cannot_reach_a_booking_of_another_salon(
        self, other_salon, admin_a, booking_of_b
    ):
        """Names salon B — where the booking really lives — while holding
        a grant only in A."""
        resp = _cancel(_api(admin_a, tenant_slug=other_salon.slug), booking_of_b)

        assert resp.status_code == 403, resp.data

    def test_a_booking_of_another_salon_is_not_found_not_forbidden(
        self, salon, admin_a, booking_of_b
    ):
        """Correctly authorised for salon A, pointing at a row in B. 404,
        so the surface does not confirm which ids exist elsewhere."""
        resp = _cancel(_api(admin_a, tenant_slug=salon.slug), booking_of_b)

        assert resp.status_code == 404, resp.data
        booking_of_b.refresh_from_db()
        assert booking_of_b.status == Appointment.Status.CONFIRMED

    def test_no_tenant_header_authorises_nothing(self, admin_a, booking):
        """``IsTenantAdmin`` fails closed with no addressed tenant — a
        grant somewhere is never a grant everywhere."""
        resp = _cancel(_api(admin_a, tenant_slug=None), booking)

        assert resp.status_code == 403, resp.data


@pytest.mark.django_db(transaction=True)
class TestTheCredentialAloneIsNotAuthority:
    def test_an_unknown_external_id_cannot_write(self, salon, booking):
        """``resolve_external_user`` mints a proxy client for an id it has
        never seen. That account holds no grant, so a leaked token plus an
        invented id writes nothing."""
        client = APIClient()
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {SERVICE_TOKEN}"
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = "bot:max:a1297ghost"
        client.defaults["HTTP_X_TENANT"] = salon.slug
        client.defaults["HTTP_X_IDEMPOTENCY_KEY"] = "a1297-ghost"

        resp = _cancel(client, booking)

        assert resp.status_code == 403, resp.data

    def test_a_wrong_token_cannot_write(self, salon, admin_a, booking):
        client = APIClient()
        client.defaults["HTTP_AUTHORIZATION"] = "Bearer not-the-token"
        client.defaults["HTTP_X_EXTERNAL_USER_ID"] = admin_a.username
        client.defaults["HTTP_X_TENANT"] = salon.slug
        client.defaults["HTTP_X_IDEMPOTENCY_KEY"] = "a1297-wrong"

        resp = _cancel(client, booking)

        assert resp.status_code == 403, resp.data


@pytest.mark.django_db(transaction=True)
class TestRescheduleStillCannotChangeTheMaster:
    """В-3. The invariant the audit found held by construction and by no
    test at all: nothing in the reschedule contract names a master, so
    adding one to the DTO or widening the ``update_fields`` allowlist
    would have passed silently.
    """

    def test_the_master_is_unchanged_by_a_reschedule(
        self, salon, admin_a, booking, master
    ):
        resp = _reschedule(_api(admin_a, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.specialist_id == master.id

    def test_a_specialist_id_in_the_body_is_ignored(
        self, salon, admin_a, booking, master, other_master
    ):
        """Belt and braces on the serializer's field list. If a future
        edit made the body pass through, this is what would catch it."""
        new_start = booking.start_datetime + timedelta(hours=2)

        resp = _api(admin_a, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/appointments/{booking.id}/reschedule/",
            {
                "new_start_datetime": new_start.isoformat(),
                "expected_version": booking.version,
                "specialist_id": str(other_master.id),
            },
            format="json",
        )

        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.specialist_id == master.id
