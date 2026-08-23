"""DRF-1297 В-4 — closing a date over a live booking is refused.

Two of the four ways a salon can reduce availability used to succeed
silently over a booked client: marking a master's date "not working"
returned 200, and closing the salon returned 201. The audit found no
test asserting either behaviour — not that the check existed, and not
that it was absent — so the silence was never a recorded decision.

These are the two the owner's ruling puts behind a hard 409, and they
share one property that makes a plain overlap count the *correct and
complete* test: the affected range is a calendar date. Nothing here
needs to know what the master's hours were before.

The two that are NOT here, and must not quietly arrive later:

* shrinking the weekly template (``PUT``/``PATCH .../schedule/``);
* trimming the hours of a *working-day* date override.

Both ask "which bookings fall outside the new frame", which needs the
old frame to compare against — a booking outside working hours is legal
(walk-ins and salon-made bookings skip the frame check on purpose), so
"outside the new frame" proves nothing by itself.
:class:`TestWhatIsDeliberatelyNotGuarded` pins that gap as a decision
rather than leaving it as an accident, so the day it is closed, a test
has to be changed on purpose.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.models import (
    Appointment,
    SpecialistScheduleException,
    SpecialistWorkingHours,
    TenantClosure,
)
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

MSK = ZoneInfo("Europe/Moscow")
# A Thursday, far enough out that "today" never drifts into it.
TARGET = date(2026, 12, 3)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="g1297", name="Guard Salon")


@pytest.fixture
def admin(salon):
    user = User.objects.create_user(
        username="g1297_admin", password="x", role="client",
        phone="+79995301001",
    )
    TenantUserRelationship.objects.create(
        user=user, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return user


def _make_master(salon, *, username, phone, name, tz="Europe/Moscow"):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.tenant = salon
    profile.timezone = tz
    profile.save()
    return profile


@pytest.fixture
def master(salon):
    return _make_master(
        salon, username="g1297_m1", phone="+79995301002", name="Ольга",
    )


@pytest.fixture
def other_master(salon):
    """A second master of the same salon — a closure covers them too."""
    return _make_master(
        salon, username="g1297_m2", phone="+79995301003", name="Денис",
    )


@pytest.fixture
def customer(salon):
    user = User.objects.create_user(
        username="g1297_client", password="x", role="client",
        phone="+79995301004", first_name="Анна",
    )
    TenantUserRelationship.objects.create(
        user=user, tenant=salon,
        role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
    )
    return user


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="G1297", slug="g1297-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _booking(
    salon, customer, master, service, *,
    at_local=time(14, 0), on=TARGET, status=None,
):
    """One appointment, placed by the master's local clock.

    Local, not UTC, because the guard's whole job is converting a date a
    human named into the instants that date covers for this master.
    """
    start = datetime.combine(on, at_local, tzinfo=MSK)
    return Appointment.objects.create(
        tenant=salon,
        client=customer,
        specialist=master,
        service=service,
        salon_service=None,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status or Appointment.Status.CONFIRMED,
        price=Decimal("2000.00"),
    )


def _api(user, salon) -> APIClient:
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_X_TENANT"] = salon.slug
    client.force_authenticate(user=user)
    return client


def _exceptions_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/schedule-exceptions/"


def _closures_url() -> str:
    return "/api/v1/tenants/me/closures/"


def _mark_not_working(api, master, on=TARGET):
    return api.put(
        _exceptions_url(master),
        {"date": on.isoformat(), "is_working_day": False},
        format="json",
    )


def _close_salon(api, on=TARGET, start=None, end=None):
    body = {"date": on.isoformat()}
    if start and end:
        body["start_time"] = start
        body["end_time"] = end
    return api.post(_closures_url(), body, format="json")


@pytest.mark.django_db
class TestDateOverrideNonWorking:
    """PUT .../schedule-exceptions/ with is_working_day=false."""

    def test_it_still_succeeds_on_an_empty_day(self, salon, admin, master):
        """The guard must not turn the ordinary case into a refusal —
        this is the day a master is simply off and nobody is booked."""
        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 200, resp.data
        assert SpecialistScheduleException.objects.filter(
            specialist=master, date=TARGET, is_working_day=False,
        ).exists()

    def test_it_is_refused_over_a_live_booking(
        self, salon, admin, master, customer, service
    ):
        """Used to return 200 and strand the client silently."""
        _booking(salon, customer, master, service)

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 409, resp.data
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"

    def test_nothing_is_written_when_it_is_refused(
        self, salon, admin, master, customer, service
    ):
        """A refusal that still saved the override would be worse than
        no guard: the client is stranded AND the operator was told no."""
        _booking(salon, customer, master, service)

        _mark_not_working(_api(admin, salon), master)

        assert not SpecialistScheduleException.objects.filter(
            specialist=master, date=TARGET,
        ).exists()

    def test_an_existing_override_is_not_replaced_when_refused(
        self, salon, admin, master, customer, service
    ):
        """PUT is an upsert. The refusal must leave the previous row
        exactly as it was, not half-applied."""
        SpecialistScheduleException.objects.create(
            specialist=master, date=TARGET, is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
        )
        _booking(salon, customer, master, service)

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 409, resp.data
        row = SpecialistScheduleException.objects.get(
            specialist=master, date=TARGET,
        )
        assert row.is_working_day is True
        assert row.start_time == time(10, 0)

    def test_a_cancelled_booking_does_not_block(
        self, salon, admin, master, customer, service
    ):
        """Only ACTIVE_BOOKING_STATUSES count — the same set the time-off
        refusal has always used. A cancelled row strands nobody."""
        _booking(
            salon, customer, master, service,
            status=Appointment.Status.CANCELLED,
        )

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 200, resp.data

    def test_a_booking_on_another_date_does_not_block(
        self, salon, admin, master, customer, service
    ):
        """The affected range is this date and only this date. A guard
        that reached wider would refuse edits it has no business in."""
        _booking(salon, customer, master, service, on=TARGET + timedelta(days=1))

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 200, resp.data

    def test_a_booking_of_another_master_does_not_block(
        self, salon, admin, master, other_master, customer, service
    ):
        _booking(salon, customer, other_master, service)

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 200, resp.data

    def test_a_late_evening_booking_is_caught(
        self, salon, admin, master, customer, service
    ):
        """21:00 MSK is 18:00 UTC — the same calendar date either way.
        Included because the window is built from the master's local
        midnight, and a UTC-built window would have missed the mirror
        case below."""
        _booking(salon, customer, master, service, at_local=time(21, 0))

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 409, resp.data

    def test_an_early_morning_booking_is_caught(
        self, salon, admin, master, customer, service
    ):
        """01:00 MSK on the 3rd is 22:00 UTC on the 2nd. A guard that
        used UTC dates would let this one through — the client would be
        stranded by an edit the operator was told had succeeded."""
        _booking(salon, customer, master, service, at_local=time(1, 0))

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 409, resp.data

    def test_a_booking_outside_working_hours_still_blocks(
        self, salon, admin, master, customer, service
    ):
        """Deliberate. A salon-made or walk-in booking may legally sit
        outside the weekly frame, and closing the date strands it just
        the same. The guard asks "is anyone booked", never "was this
        booking inside the hours"."""
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=TARGET.weekday(),
            is_working_day=True, start_time=time(10, 0), end_time=time(19, 0),
        )
        _booking(salon, customer, master, service, at_local=time(20, 30))

        resp = _mark_not_working(_api(admin, salon), master)

        assert resp.status_code == 409, resp.data


@pytest.mark.django_db
class TestTenantClosure:
    """POST /api/v1/tenants/me/closures/."""

    def test_it_still_succeeds_on_an_empty_day(self, salon, admin, master):
        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 201, resp.data
        assert TenantClosure.objects.filter(tenant=salon, date=TARGET).exists()

    def test_a_full_day_closure_is_refused_over_a_live_booking(
        self, salon, admin, master, customer, service
    ):
        """Used to return 201 and close the salon over a booked client."""
        _booking(salon, customer, master, service)

        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 409, resp.data
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"
        assert not TenantClosure.objects.filter(tenant=salon).exists()

    def test_any_master_of_the_salon_blocks_it(
        self, salon, admin, master, other_master, customer, service
    ):
        """One row closes the salon for every master it has, so every
        master has to be asked — not just the first one found."""
        _booking(salon, customer, other_master, service)

        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 409, resp.data

    def test_a_partial_closure_only_refuses_inside_its_own_window(
        self, salon, admin, master, customer, service
    ):
        """A lunchtime closure must not be refused by an evening booking:
        the affected range is the hours named, not the whole day."""
        _booking(salon, customer, master, service, at_local=time(18, 0))

        resp = _close_salon(_api(admin, salon), start="12:00", end="14:00")

        assert resp.status_code == 201, resp.data

    def test_a_partial_closure_is_refused_by_a_booking_inside_it(
        self, salon, admin, master, customer, service
    ):
        _booking(salon, customer, master, service, at_local=time(13, 0))

        resp = _close_salon(_api(admin, salon), start="12:00", end="14:00")

        assert resp.status_code == 409, resp.data

    def test_a_booking_in_another_salon_does_not_block(
        self, db, salon, admin, master, customer, service
    ):
        """The closure is tenant-scoped; so is the question it asks."""
        other = Tenant.objects.create(slug="g1297-b", name="Other")
        outsider = _make_master(
            other, username="g1297_out", phone="+79995301005", name="Инна",
        )
        _booking(other, customer, outsider, service)

        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 201, resp.data

    def test_a_cancelled_booking_does_not_block(
        self, salon, admin, master, customer, service
    ):
        _booking(
            salon, customer, master, service,
            status=Appointment.Status.CANCELLED,
        )

        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 201, resp.data

    def test_the_duplicate_closure_409_still_says_something_else(
        self, salon, admin, master
    ):
        """Two different 409s live on this endpoint now. They must stay
        distinguishable — a client that renders "someone is booked" for
        "you already closed this date" is lying to the operator."""
        _close_salon(_api(admin, salon))

        resp = _close_salon(_api(admin, salon))

        assert resp.status_code == 409, resp.data
        assert resp.data["error"]["code"] == "CLOSURE_EXISTS"

    def test_reopening_is_never_refused(
        self, salon, admin, master, customer, service
    ):
        """DELETE widens availability. Guarding it would refuse the very
        action that resolves the conflict."""
        api = _api(admin, salon)
        created = _close_salon(api)
        _booking(salon, customer, master, service)

        resp = api.delete(f"{_closures_url()}{created.data['data']['id']}/")

        assert resp.status_code == 204


@pytest.mark.django_db
class TestWhatIsDeliberatelyNotGuarded:
    """The recorded shape of the gap the owner's ruling leaves open.

    These assert current behaviour, not desired behaviour. They exist so
    that closing the weekly-shrink gap is a deliberate act — someone has
    to come here and change an assertion — rather than something that
    happens by accident and is discovered in production.
    """

    def test_shrinking_the_weekly_template_is_not_guarded(
        self, salon, admin, master, customer, service
    ):
        """Needs old-frame-vs-new over the booking horizon, plus
        transaction guarantees this endpoint does not have. Out of scope
        by ruling; Ayla must not offer weekly edits as a supported write
        until it is closed."""
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=TARGET.weekday(),
            is_working_day=True, start_time=time(10, 0), end_time=time(19, 0),
        )
        _booking(salon, customer, master, service)

        resp = _api(admin, salon).patch(
            f"/api/v1/tenants/me/masters/{master.id}/schedule/",
            {"schedule": [{
                "day_of_week": TARGET.weekday(), "is_working_day": False,
                "start_time": None, "end_time": None,
            }]},
            format="json",
        )

        assert resp.status_code == 200, resp.data

    def test_trimming_a_working_day_override_is_not_guarded(
        self, salon, admin, master, customer, service
    ):
        """Same comparison problem, one date instead of a template. The
        booking below sits outside the proposed hours, but so might a
        booking the salon placed there on purpose — the endpoint cannot
        yet tell the two apart."""
        _booking(salon, customer, master, service, at_local=time(18, 0))

        resp = _api(admin, salon).put(
            _exceptions_url(master),
            {
                "date": TARGET.isoformat(), "is_working_day": True,
                "start_time": "10:00", "end_time": "14:00",
            },
            format="json",
        )

        assert resp.status_code == 200, resp.data
