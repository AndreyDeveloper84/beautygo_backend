"""Salon-admin schedule surface — /api/v1/tenants/me/masters/... (DRF-1062).

The point of this surface is that a salon can fix its own schedule. The
point of these tests is that it can fix only its OWN: the master is taken
from the URL, but the tenant is taken from middleware, so a master in
another salon must be invisible rather than merely forbidden.

Covered:
- an administrator edits a master they do not own the session of;
- the pro-app surface for the master themselves is untouched;
- cross-tenant addressing returns 404, not 403 (no id enumeration);
- customer / staff / no-tenant-context cannot reach it;
- platform staff may address any tenant, but must still name one;
- per-date exceptions and salon closures round-trip;
- an absence over live bookings still refuses with 409.
"""
from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient

from appointments.models import (
    Appointment,
    SpecialistScheduleException,
    SpecialistTimeOff,
    SpecialistWorkingHours,
    TenantClosure,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


def _make_user(*, username, role="client", phone="", platform=False):
    user = User.objects.create_user(
        username=username, password="x", role=role, phone=phone,
    )
    if platform:
        user.is_platform_admin = True
        user.save(update_fields=["is_platform_admin"])
    return user


def _client_as(user, tenant=None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    if tenant is not None:
        c.defaults["HTTP_X_TENANT"] = tenant.slug
    c.force_authenticate(user=user)
    return c


def _grant(user, tenant, role):
    TenantUserRelationship.objects.filter(user=user).delete()
    return TenantUserRelationship.objects.create(
        user=user, tenant=tenant, role=role, is_active=True,
    )


def _master(username, tenant, phone):
    user = _make_user(username=username, role="specialist", phone=phone)
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = username
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="s1062-a", name="Салон А")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="s1062-b", name="Салон Б")


@pytest.fixture
def admin(db, salon):
    user = _make_user(username="s1062_admin", role="admin", phone="+79991062001")
    _grant(user, salon, TenantUserRelationship.Role.ADMIN)
    return user


@pytest.fixture
def master(db, salon):
    return _master("s1062_master", salon, "+79991062002")


@pytest.fixture
def foreign_master(db, other_salon):
    return _master("s1062_foreign", other_salon, "+79991062003")


def _schedule_url(specialist_id) -> str:
    return f"/api/v1/tenants/me/masters/{specialist_id}/schedule/"


def _full_week(start="10:00", end="19:00"):
    return {
        "schedule": [
            {
                "day_of_week": d,
                "is_working_day": d < 5,
                "start_time": start if d < 5 else None,
                "end_time": end if d < 5 else None,
            }
            for d in range(7)
        ]
    }


# ---------------------------------------------------------------------------
# The capability that did not exist
# ---------------------------------------------------------------------------

class TestAdminEditsAnotherMastersSchedule:
    def test_admin_replaces_the_week(self, admin, master, salon):
        resp = _client_as(admin, salon).put(
            _schedule_url(master.id), _full_week(), format="json",
        )

        assert resp.status_code == 200
        rows = SpecialistWorkingHours.objects.filter(specialist=master)
        assert rows.count() == 7
        assert rows.get(day_of_week=6).is_working_day is False
        assert rows.get(day_of_week=0).start_time == time(10, 0)

    def test_admin_reads_the_week(self, admin, master, salon):
        resp = _client_as(admin, salon).get(_schedule_url(master.id))

        assert resp.status_code == 200
        assert len(resp.data["data"]) == 7

    def test_admin_patches_one_day_with_a_lunch_break(self, admin, master, salon):
        client = _client_as(admin, salon)
        client.put(_schedule_url(master.id), _full_week(), format="json")

        resp = client.patch(
            _schedule_url(master.id),
            {"schedule": [{
                "day_of_week": 0,
                "is_working_day": True,
                "start_time": "10:00",
                "end_time": "19:00",
                "break_start": "13:00",
                "break_end": "14:00",
            }]},
            format="json",
        )

        assert resp.status_code == 200
        row = SpecialistWorkingHours.objects.get(specialist=master, day_of_week=0)
        assert row.break_start == time(13, 0)

    def test_inherited_validation_still_applies(self, admin, master, salon):
        resp = _client_as(admin, salon).patch(
            _schedule_url(master.id),
            {"schedule": [{
                "day_of_week": 0,
                "is_working_day": True,
                "start_time": "10:00",
                "end_time": "19:00",
                "break_start": "09:00",
                "break_end": "09:30",
            }]},
            format="json",
        )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

class TestTenantBoundary:
    def test_foreign_master_is_not_found_not_forbidden(
        self, admin, foreign_master, salon,
    ):
        """404 rather than 403: the surface must not confirm which ids exist."""
        resp = _client_as(admin, salon).get(_schedule_url(foreign_master.id))

        assert resp.status_code == 404

    def test_foreign_master_cannot_be_written_either(
        self, admin, foreign_master, salon,
    ):
        resp = _client_as(admin, salon).put(
            _schedule_url(foreign_master.id), _full_week(), format="json",
        )

        assert resp.status_code == 404
        assert not SpecialistWorkingHours.objects.filter(
            specialist=foreign_master,
        ).exists()

    def test_without_tenant_context_there_is_no_admin_scope(self, admin, master):
        resp = _client_as(admin, tenant=None).get(_schedule_url(master.id))

        assert resp.status_code == 403


class TestRoleEscalation:
    @pytest.mark.parametrize(
        "role",
        [TenantUserRelationship.Role.CUSTOMER, TenantUserRelationship.Role.STAFF],
    )
    def test_non_admin_relationships_are_refused(self, db, salon, master, role):
        user = _make_user(username=f"s1062_{role}", phone=f"+7999106201{len(role)}")
        _grant(user, salon, role)

        resp = _client_as(user, salon).get(_schedule_url(master.id))

        assert resp.status_code == 403

    def test_a_master_cannot_edit_a_colleague(self, db, salon, master):
        colleague = _master("s1062_colleague", salon, "+79991062009")

        resp = _client_as(colleague.user, salon).put(
            _schedule_url(master.id), _full_week(), format="json",
        )

        assert resp.status_code == 403


class TestPlatformAdmin:
    def test_platform_staff_may_address_any_tenant(self, db, other_salon, foreign_master):
        operator = _make_user(
            username="s1062_platform", phone="+79991062020", platform=True,
        )

        resp = _client_as(operator, other_salon).put(
            _schedule_url(foreign_master.id), _full_week(), format="json",
        )

        assert resp.status_code == 200

    def test_platform_staff_must_still_name_a_tenant(self, db, foreign_master):
        """Cross-tenant reach, not tenant-blindness — the DRF-1025 defect."""
        operator = _make_user(
            username="s1062_platform2", phone="+79991062021", platform=True,
        )

        resp = _client_as(operator, tenant=None).get(_schedule_url(foreign_master.id))

        assert resp.status_code == 403

    def test_platform_staff_addressing_the_wrong_tenant_still_misses(
        self, db, salon, foreign_master,
    ):
        operator = _make_user(
            username="s1062_platform3", phone="+79991062022", platform=True,
        )

        resp = _client_as(operator, salon).get(_schedule_url(foreign_master.id))

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The new entities
# ---------------------------------------------------------------------------

class TestScheduleExceptions:
    def _url(self, specialist_id):
        return f"/api/v1/tenants/me/masters/{specialist_id}/schedule-exceptions/"

    def test_admin_sets_shorter_hours_for_one_date(self, admin, master, salon):
        friday = date.today() + timedelta(days=14)

        resp = _client_as(admin, salon).put(
            self._url(master.id),
            {
                "date": friday.isoformat(),
                "is_working_day": True,
                "start_time": "10:00",
                "end_time": "15:00",
                "note": "в эту пятницу до 15:00",
            },
            format="json",
        )

        assert resp.status_code == 200
        row = SpecialistScheduleException.objects.get(specialist=master, date=friday)
        assert row.end_time == time(15, 0)

    def test_setting_the_same_date_twice_replaces_it(self, admin, master, salon):
        day = date.today() + timedelta(days=15)
        client = _client_as(admin, salon)
        body = {"date": day.isoformat(), "is_working_day": False}

        assert client.put(self._url(master.id), body, format="json").status_code == 200
        assert client.put(self._url(master.id), body, format="json").status_code == 200

        assert SpecialistScheduleException.objects.filter(
            specialist=master, date=day,
        ).count() == 1

    def test_a_non_working_exception_may_not_carry_times(self, admin, master, salon):
        day = date.today() + timedelta(days=16)

        resp = _client_as(admin, salon).put(
            self._url(master.id),
            {"date": day.isoformat(), "is_working_day": False, "start_time": "10:00"},
            format="json",
        )

        assert resp.status_code == 400

    def test_deleting_falls_back_to_the_weekly_template(self, admin, master, salon):
        day = date.today() + timedelta(days=17)
        client = _client_as(admin, salon)
        client.put(
            self._url(master.id),
            {"date": day.isoformat(), "is_working_day": False},
            format="json",
        )

        resp = client.delete(f"{self._url(master.id)}{day.isoformat()}/")

        assert resp.status_code == 204
        assert not SpecialistScheduleException.objects.filter(
            specialist=master, date=day,
        ).exists()


class TestTenantClosures:
    URL = "/api/v1/tenants/me/closures/"

    def test_admin_closes_the_salon_for_a_holiday(self, admin, salon):
        holiday = date.today() + timedelta(days=20)

        resp = _client_as(admin, salon).post(
            self.URL,
            {"date": holiday.isoformat(), "reason": "праздник"},
            format="json",
        )

        assert resp.status_code == 201
        row = TenantClosure.objects.get(tenant=salon, date=holiday)
        assert row.is_full_day

    def test_one_row_no_per_master_fan_out(self, admin, salon, master):
        holiday = date.today() + timedelta(days=21)

        _client_as(admin, salon).post(
            self.URL, {"date": holiday.isoformat()}, format="json",
        )

        assert TenantClosure.objects.filter(tenant=salon).count() == 1
        assert SpecialistTimeOff.objects.count() == 0

    def test_closing_the_same_day_twice_conflicts(self, admin, salon):
        holiday = date.today() + timedelta(days=22)
        client = _client_as(admin, salon)
        body = {"date": holiday.isoformat()}

        assert client.post(self.URL, body, format="json").status_code == 201
        assert client.post(self.URL, body, format="json").status_code == 409

    def test_partial_closure_requires_both_ends(self, admin, salon):
        resp = _client_as(admin, salon).post(
            self.URL,
            {
                "date": (date.today() + timedelta(days=23)).isoformat(),
                "start_time": "10:00",
            },
            format="json",
        )

        assert resp.status_code == 400

    def test_closures_of_other_salons_are_not_listed(self, admin, salon, other_salon):
        TenantClosure.objects.create(
            tenant=other_salon, date=date.today() + timedelta(days=24),
        )

        resp = _client_as(admin, salon).get(self.URL)

        assert resp.status_code == 200
        assert resp.data["data"] == []

    def test_reopening_removes_the_closure(self, admin, salon):
        holiday = date.today() + timedelta(days=25)
        client = _client_as(admin, salon)
        created = client.post(self.URL, {"date": holiday.isoformat()}, format="json")

        resp = client.delete(f"{self.URL}{created.data['data']['id']}/")

        assert resp.status_code == 204
        assert not TenantClosure.objects.filter(tenant=salon).exists()


class TestAbsenceOverLiveBookings:
    def test_still_refuses_with_409_until_the_bookings_are_resolved(
        self, admin, master, salon, db,
    ):
        """The protection stays until DRF-1062 §C gives admins a way to
        decide what happens to the people already booked."""
        from django.utils import timezone

        from services.models import Service, ServiceCategory

        category = ServiceCategory.objects.create(name="C1062", slug="c1062")
        service = Service.objects.create(
            specialist=master, category=category, name="S1062",
            price="1000.00", duration_minutes=60, is_active=True,
        )
        client_user = _make_user(username="s1062_client", phone="+79991062030")
        start = timezone.now() + timedelta(days=3)
        Appointment.objects.create(
            client=client_user, specialist=master, service=service,
            start_datetime=start, end_datetime=start + timedelta(minutes=60),
            price=service.price, status=Appointment.Status.CONFIRMED,
            snapshot_service_name=service.name, snapshot_price=service.price,
            snapshot_duration_minutes=60,
        )

        resp = _client_as(admin, salon).post(
            f"/api/v1/tenants/me/masters/{master.id}/time-off/",
            {
                "start_at": (start - timedelta(hours=1)).isoformat(),
                "end_at": (start + timedelta(hours=2)).isoformat(),
                "reason": "болезнь",
            },
            format="json",
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"
