"""DRF-2826 — the salon's receptionist: the booking desk, not the owner's powers.

Owner decision 06.10 (handoff §7), boundaries by the main window:

* ALLOWED — read the schedule and the day; the whole booking cycle (create,
  reschedule, cancel; complete / no-show ride the same permission list);
  find the customer being booked;
* REFUSED — another salon; moving availability (weekly template, time off,
  date exceptions, closures); managing roles.

Every request uses the bot's real credential set (service Bearer +
``X-External-User-ID`` + ``X-Tenant``), or a human JWT where the surface's
write path is the console's. Authority comes from the relationship row
only: the receptionist here is a ``client`` account with a ``receptionist``
grant, the same shape as the pilot's administrator.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from appointments.models import Appointment
from services.models import SalonService, Service, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "receptionist-rbac-token-under-test"  # pragma: allowlist secret
RECEPTIONIST = TenantUserRelationship.Role.RECEPTIONIST


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="r2826-a", name="Desk Salon A")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="r2826-b", name="Desk Salon B")


def _user(username, *, phone, role="client"):
    return User.objects.create_user(username=username, password="x", role=role, phone=phone)


def _grant(user, tenant, role, *, active=True):
    return TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=role, is_active=active,
    )


@pytest.fixture
def desk_a(salon):
    user = _user("bot:max:r2826desk", phone="+79995826001")
    _grant(user, salon, RECEPTIONIST)
    return user


@pytest.fixture
def desk_b(other_salon):
    """Receptionist of ANOTHER salon — the impersonation attempt."""
    user = _user("bot:max:r2826deskb", phone="+79995826002")
    _grant(user, other_salon, RECEPTIONIST)
    return user


@pytest.fixture
def revoked_desk(salon):
    user = _user("bot:max:r2826revoked", phone="+79995826003")
    _grant(user, salon, RECEPTIONIST, active=False)
    return user


@pytest.fixture
def admin_a(salon):
    user = _user("bot:max:r2826admin", phone="+79995826004")
    _grant(user, salon, TenantUserRelationship.Role.ADMIN)
    return user


def _make_master(tenant, *, username, phone):
    user = _user(username, phone=phone, role="specialist")
    user.tenant = tenant
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = "Ольга"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.tenant = tenant
    profile.save()
    return profile


@pytest.fixture
def master(salon):
    return _make_master(salon, username="r2826_m", phone="+79995826005")


@pytest.fixture
def client_user(salon):
    user = _user("bot:max:r2826client", phone="+79995826006")
    _grant(user, salon, TenantUserRelationship.Role.CUSTOMER)
    return user


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="R2826", slug="r2826-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def salon_service(master):
    """A salon service that answers the health question explicitly «no»
    (§100: the marketplace layer is closed fail-closed) — same shape as the
    DRF-1063 manual-ops nodes. The subject here is who may book, not the gate."""
    tenant_id = SpecialistProfile.objects.values_list("tenant_id", flat=True).get(pk=master.pk)
    category = ServiceCategory.objects.create(name="R2826 S", slug="r2826-s")
    svc = SalonService.objects.create(
        tenant_id=tenant_id, category=category, name="Массаж", duration_minutes=60,
        base_price=Decimal("2000.00"), is_active=True, requires_health_check=False,
    )
    SpecialistService.objects.create(
        salon_service=svc, specialist=master, duration_minutes=60,
        price=Decimal("2000.00"), buffer_after_minutes=0, is_active=True,
    )
    return svc


@pytest.fixture
def booking(salon, client_user, master, service):
    start = (datetime.now(tz=dt_timezone.utc) + timedelta(days=3)).replace(
        minute=0, second=0, microsecond=0,
    )
    return Appointment.objects.create(
        tenant=salon, client=client_user, specialist=master, service=service,
        salon_service=None, start_datetime=start, end_datetime=start + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED, version=1, price=Decimal("2000.00"),
    )


def _bot(user, *, tenant_slug) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {SERVICE_TOKEN}"
    client.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    client.defaults["HTTP_X_IDEMPOTENCY_KEY"] = f"r2826-{user.username}"
    client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _human(user, *, tenant_slug) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RefreshToken.for_user(user).access_token}"
    client.defaults["HTTP_X_TENANT"] = tenant_slug
    return client


def _reschedule(api, booking):
    return api.post(
        f"/api/v1/tenants/me/appointments/{booking.id}/reschedule/",
        {
            "new_start_datetime": (booking.start_datetime + timedelta(hours=2)).isoformat(),
            "expected_version": booking.version,
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
    start = (datetime.now(tz=dt_timezone.utc) + timedelta(days=5)).replace(
        minute=0, second=0, microsecond=0,
    )
    return api.post(
        "/api/v1/tenants/me/appointments/",
        {
            "specialist_id": str(master.id),
            "service_id": str(service.id),
            "start_datetime": start.isoformat(),
            "client_id": str(client_user.id),
        },
        format="json",
    )


def _full_week():
    return [
        {
            "day_of_week": day,
            "is_working_day": day < 5,
            "start_time": "10:00" if day < 5 else None,
            "end_time": "19:00" if day < 5 else None,
            "break_start": None,
            "break_end": None,
        }
        for day in range(7)
    ]


# --- allowed -------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestTheReceptionistRunsTheBookingDesk:
    def test_c1_creates_a_booking(self, salon, desk_a, master, salon_service, client_user):
        resp = _create(_bot(desk_a, tenant_slug=salon.slug), master, salon_service, client_user)

        assert resp.status_code == 201, resp.data

    def test_c2_reschedules(self, salon, desk_a, booking):
        resp = _reschedule(_bot(desk_a, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data

    def test_c3_cancels_and_it_is_recorded_as_the_salon(self, salon, desk_a, booking):
        resp = _cancel(_bot(desk_a, tenant_slug=salon.slug), booking)

        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CANCELLED

    def test_c4_reads_the_day(self, salon, desk_a, booking):
        resp = _bot(desk_a, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/day/", {"date": booking.start_datetime.date().isoformat()},
        )

        assert resp.status_code == 200, resp.data

    def test_c5_finds_the_customer_to_book(self, salon, desk_a, client_user):
        resp = _bot(desk_a, tenant_slug=salon.slug).get(
            "/api/v1/tenants/me/customers/", {"q": "r2826"},
        )

        assert resp.status_code != 403, resp.data

    def test_c6_reads_the_schedule(self, salon, desk_a, master):
        resp = _bot(desk_a, tenant_slug=salon.slug).get(
            f"/api/v1/tenants/me/masters/{master.id}/schedule/",
        )

        assert resp.status_code == 200, resp.data


# --- refused -------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAnotherSalonIsRefused:
    @pytest.mark.parametrize("operation", ["reschedule", "cancel", "create", "day"])
    def test_r1_a_receptionist_of_salon_b_cannot_act_in_a(
        self, salon, desk_b, master, service, client_user, booking, operation,
    ):
        api = _bot(desk_b, tenant_slug=salon.slug)
        if operation == "reschedule":
            resp = _reschedule(api, booking)
        elif operation == "cancel":
            resp = _cancel(api, booking)
        elif operation == "create":
            resp = _create(api, master, service, client_user)
        else:
            resp = api.get("/api/v1/tenants/me/day/", {"date": date.today().isoformat()})

        assert resp.status_code == 403, resp.data
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_r2_a_revoked_receptionist_is_refused(self, salon, revoked_desk, booking):
        resp = _cancel(_bot(revoked_desk, tenant_slug=salon.slug), booking)

        assert resp.status_code == 403, resp.data


@pytest.mark.django_db(transaction=True)
class TestAvailabilityAndRolesStayWithTheOwner:
    def test_r3_the_weekly_template_refuses_the_receptionist_and_admits_the_admin(
        self, salon, desk_a, admin_a, master,
    ):
        url = f"/api/v1/tenants/me/masters/{master.id}/schedule/"

        refused = _human(desk_a, tenant_slug=salon.slug).put(url, _full_week(), format="json")
        admitted = _human(admin_a, tenant_slug=salon.slug).put(url, _full_week(), format="json")

        assert refused.status_code == 403, refused.data
        assert admitted.status_code != 403, admitted.data  # positive control

    def test_r4_time_off_and_closures_refuse_a_write(self, salon, desk_a, master):
        api = _human(desk_a, tenant_slug=salon.slug)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        time_off = api.post(
            f"/api/v1/tenants/me/masters/{master.id}/time-off/",
            {"start_date": tomorrow, "end_date": tomorrow, "reason": "test"},
            format="json",
        )
        closure = api.post(
            "/api/v1/tenants/me/closures/",
            {"start_date": tomorrow, "end_date": tomorrow, "reason": "test"},
            format="json",
        )

        assert (time_off.status_code, closure.status_code) == (403, 403)

    def test_r5_managing_roles_is_refused(self, salon, desk_a, client_user):
        resp = _human(desk_a, tenant_slug=salon.slug).post(
            f"/api/v1/tenants/me/relationships/{client_user.id}/revoke/",
            {"reason": "test"},
            format="json",
        )

        assert resp.status_code == 403, resp.data


# --- the permission classes themselves ----------------------------------------


@pytest.mark.django_db
class TestThePermissionClasses:
    def _req(self, user, tenant, method):
        return SimpleNamespace(user=user, tenant=tenant, method=method)

    def test_p1_booking_desk_admits_admin_and_receptionist_only(
        self, salon, desk_a, admin_a, client_user,
    ):
        from users.permissions import IsTenantBookingDesk

        perm = IsTenantBookingDesk()
        assert perm.has_permission(self._req(desk_a, salon, "POST"), None) is True
        assert perm.has_permission(self._req(admin_a, salon, "POST"), None) is True
        assert perm.has_permission(self._req(client_user, salon, "POST"), None) is False

    def test_p2_schedule_read_admits_receptionist_on_safe_methods_only(
        self, salon, desk_a, admin_a,
    ):
        from users.permissions import IsTenantAdminOrBookingDeskRead

        perm = IsTenantAdminOrBookingDeskRead()
        assert perm.has_permission(self._req(desk_a, salon, "GET"), None) is True
        for method in ("PUT", "PATCH", "POST", "DELETE"):
            assert perm.has_permission(self._req(desk_a, salon, method), None) is False, method
        assert perm.has_permission(self._req(admin_a, salon, "PUT"), None) is True

    def test_p3_is_tenant_admin_still_refuses_the_receptionist(self, salon, desk_a):
        """Roles, linking, the Mini App admin exchange keep IsTenantAdmin."""

        from users.permissions import IsTenantAdmin

        assert IsTenantAdmin().has_permission(self._req(desk_a, salon, "GET"), None) is False
