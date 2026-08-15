"""DRF-1063 — the salon's day journal.

The audit of 2026-08-14 found that a salon administrator sees nothing at
all: ``AppointmentViewSet.get_queryset`` filters to the caller's own
client or specialist rows and falls through to ``none()`` for everyone
else. This is the projection that closes that, so what needs pinning is
mostly what it must NOT do:

* not leak another salon's day;
* not print UTC where a human reads a clock (DRF-1071);
* not hand out a customer phone number (DRF-1039);
* not hide masters who have nothing booked, which would make "who is
  free at three" unanswerable.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.models import (
    Appointment, SpecialistTimeOff, SpecialistWorkingHours,
)
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

MSK = ZoneInfo("Europe/Moscow")
DAY = date(2026, 8, 19)          # a Wednesday


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="day-1063", name="Day Salon")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="day-1063-b", name="Other Day Salon")


def _master(tenant, *, username, phone, name):
    u = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    u.tenant = tenant
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = name
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = tenant
    p.save()
    return p


@pytest.fixture
def olga(salon):
    return _master(
        salon, username="day_olga", phone="+79991030641", name="Ольга",
    )


@pytest.fixture
def denis(salon):
    """A second master with nothing booked — the idle-master case."""
    return _master(
        salon, username="day_denis", phone="+79991030642", name="Денис",
    )


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Day Cat", slug="day-cat")


@pytest.fixture
def service(olga, category):
    return Service.objects.create(
        specialist=olga, category=category, name="УЗ-кавитация",
        price=Decimal("1000.00"), duration_minutes=30, is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="day_client", password="x", role="client",
        phone="+79991030643", first_name="Анна", last_name="К.",
    )


@pytest.fixture
def admin_user(db, salon):
    u = User.objects.create_user(
        username="day_admin", password="x", role="admin",
        phone="+79991030644",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


def _booking(tenant, specialist, service, client_user, *, local_hhmm="14:00"):
    hh, mm = (int(part) for part in local_hhmm.split(":"))
    start_local = datetime.combine(DAY, time(hh, mm), tzinfo=MSK)
    end_local = start_local + timedelta(minutes=service.duration_minutes)
    return Appointment.objects.create(
        client=client_user, specialist=specialist, service=service,
        tenant=tenant,
        start_datetime=start_local, end_datetime=end_local,
        price=service.price, status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
        idempotency_key=str(uuid4()),
    )


def _api(user, *, tenant_slug=None, app_type="pro") -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = app_type
    if tenant_slug:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    c.force_authenticate(user=user)
    return c


def _get(user, salon_slug, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = "/api/v1/tenants/me/day/" + (f"?{query}" if query else "")
    return _api(user, tenant_slug=salon_slug).get(url)


@pytest.mark.django_db
class TestDayJournal:

    def test_admin_sees_the_whole_salon_not_just_one_master(
        self, salon, admin_user, olga, denis, service, client_user,
    ):
        _booking(salon, olga, service, client_user)

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        assert resp.status_code == 200, resp.data
        data = resp.data["data"]
        assert data["date"] == DAY.isoformat()
        names = [m["display_name"] for m in data["masters"]]
        # Both masters, including the one with an empty day — otherwise
        # "who is free at three" has no answer.
        assert "Ольга" in names and "Денис" in names
        assert data["summary"]["masters"] == 2
        assert data["summary"]["bookings"] == 1
        assert data["summary"]["by_status"] == {"confirmed": 1}

    def test_times_are_rendered_in_the_masters_timezone(
        self, salon, admin_user, olga, service, client_user,
    ):
        """DRF-1071 in one assertion: 14:00 MSK must not read as 11:00."""
        _booking(salon, olga, service, client_user, local_hhmm="14:00")

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        booking = [
            m for m in resp.data["data"]["masters"]
            if m["display_name"] == "Ольга"
        ][0]["bookings"][0]
        assert booking["start_at_local"].startswith("2026-08-19T14:00:00")
        assert booking["start_at_local"].endswith("+03:00")
        # The UTC instant travels alongside so a machine consumer never
        # has to parse the offset back out.
        assert booking["start_at"].startswith("2026-08-19T11:00:00")

    def test_customer_is_named_but_never_phoned(
        self, salon, admin_user, olga, service, client_user,
    ):
        """DRF-1039: the salon reaches clients through Ayla.

        The operational name is in — greeting the right person is the
        point of a day journal. The number is not, and this surface must
        not become the way around that decision.
        """
        _booking(salon, olga, service, client_user)

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        booking = resp.data["data"]["masters"][0]["bookings"][0] \
            if resp.data["data"]["masters"][0]["bookings"] \
            else resp.data["data"]["masters"][1]["bookings"][0]
        assert booking["client_name"] == "Анна К."
        assert booking["client_id"]
        serialised = str(resp.data)
        assert "+79991030643" not in serialised
        assert "phone" not in serialised

    def test_working_hours_breaks_and_absences_are_shown(
        self, salon, admin_user, olga,
    ):
        SpecialistWorkingHours.objects.create(
            specialist=olga, day_of_week=DAY.weekday(), is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
            break_start=time(13, 0), break_end=time(14, 0),
        )
        SpecialistTimeOff.objects.create(
            specialist=olga,
            start_at=datetime.combine(DAY, time(16, 0), tzinfo=MSK),
            end_at=datetime.combine(DAY, time(18, 0), tzinfo=MSK),
            reason="врач",
        )

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        master = [
            m for m in resp.data["data"]["masters"]
            if m["display_name"] == "Ольга"
        ][0]
        assert master["is_working_day"] is True
        assert master["working_intervals"] == [
            {"start_local": "10:00", "end_local": "19:00"},
        ]
        assert master["breaks"] == [
            {"start_local": "13:00", "end_local": "14:00"},
        ]
        assert len(master["absences"]) == 1
        assert master["absences"][0]["reason"] == "врач"
        assert master["absences"][0]["start_at_local"].startswith(
            "2026-08-19T16:00:00",
        )

    def test_a_day_off_is_reported_as_such(self, salon, admin_user, olga):
        SpecialistWorkingHours.objects.create(
            specialist=olga, day_of_week=DAY.weekday(), is_working_day=False,
        )

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        master = [
            m for m in resp.data["data"]["masters"]
            if m["display_name"] == "Ольга"
        ][0]
        assert master["is_working_day"] is False
        assert master["working_intervals"] == []

    def test_a_booking_on_another_date_is_not_included(
        self, salon, admin_user, olga, service, client_user,
    ):
        _booking(salon, olga, service, client_user)

        resp = _get(
            admin_user, salon.slug,
            date=(DAY + timedelta(days=1)).isoformat(),
        )

        assert resp.data["data"]["summary"]["bookings"] == 0

    def test_late_evening_booking_belongs_to_the_local_day(
        self, salon, admin_user, olga, service, client_user,
    ):
        """23:30 MSK is 20:30 UTC — same day either way. The interesting
        one is the reverse: a 02:00 MSK booking is 23:00 UTC the previous
        day, and a UTC-shaped window would file it under yesterday."""
        start_local = datetime.combine(DAY, time(2, 0), tzinfo=MSK)
        Appointment.objects.create(
            client=client_user, specialist=olga, service=service,
            tenant=salon,
            start_datetime=start_local,
            end_datetime=start_local + timedelta(minutes=30),
            price=service.price, status=Appointment.Status.CONFIRMED,
            snapshot_service_name=service.name,
            snapshot_price=service.price,
            snapshot_duration_minutes=30,
            idempotency_key=str(uuid4()),
        )

        resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        assert resp.data["data"]["summary"]["bookings"] == 1

    def test_bad_date_is_a_400_not_a_silent_today(
        self, salon, admin_user, olga,
    ):
        resp = _get(admin_user, salon.slug, date="19.08.2026")

        assert resp.status_code == 400
        assert resp.data["error"]["code"] == "VALIDATION_ERROR"

    def test_date_defaults_to_today(self, salon, admin_user, olga):
        resp = _get(admin_user, salon.slug)

        assert resp.status_code == 200
        assert resp.data["data"]["date"]

    def test_query_count_does_not_grow_with_the_roster(
        self, salon, admin_user, olga, denis, service, client_user,
        django_assert_max_num_queries,
    ):
        """A day journal gets polled. The per-master slicing is done in
        Python over four result sets precisely so adding masters costs
        rows, not round-trips — this is the test that keeps it that way
        if someone later reaches for ``master.working_hours.all()``
        inside the loop."""
        _booking(salon, olga, service, client_user, local_hhmm="14:00")
        _booking(salon, denis, service, client_user, local_hhmm="15:00")
        for i in range(6):
            _master(
                salon, username=f"day_extra_{i}",
                phone=f"+7999104064{i}", name=f"Мастер {i}",
            )

        with django_assert_max_num_queries(12):
            resp = _get(admin_user, salon.slug, date=DAY.isoformat())

        assert resp.status_code == 200
        assert resp.data["data"]["summary"]["masters"] == 8


@pytest.mark.django_db
class TestDayJournalBoundaries:

    def test_another_salons_day_is_not_visible(
        self, salon, other_salon, admin_user, olga, service, client_user,
    ):
        """The tenant comes from middleware, and the admin grant is for
        one salon. Addressing another one must not work even though the
        route says "me"."""
        _booking(salon, olga, service, client_user)

        resp = _get(admin_user, other_salon.slug, date=DAY.isoformat())

        assert resp.status_code == 403

    def test_a_master_cannot_open_the_salon_journal(
        self, salon, olga, service, client_user,
    ):
        """Deliberate: the master's own day is a different projection,
        owned by a different task. This one shows colleagues' work."""
        _booking(salon, olga, service, client_user)

        resp = _get(olga.user, salon.slug, date=DAY.isoformat())

        assert resp.status_code == 403

    def test_a_client_cannot_open_it(self, salon, admin_user, client_user):
        resp = _get(client_user, salon.slug, date=DAY.isoformat())

        assert resp.status_code == 403

    def test_without_an_addressed_tenant_it_fails_closed(
        self, salon, admin_user, olga,
    ):
        resp = _api(admin_user).get("/api/v1/tenants/me/day/")

        assert resp.status_code == 403

    def test_client_app_callers_are_refused(self, salon, admin_user, olga):
        """IsProApp: a stolen admin JWT replayed from a client build
        does not reach the salon-admin surface."""
        resp = _api(admin_user, tenant_slug=salon.slug, app_type="client").get(
            "/api/v1/tenants/me/day/",
        )

        assert resp.status_code == 403
