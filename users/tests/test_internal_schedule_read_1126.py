"""GET /api/v1/internal/specialists/{id}/schedule/ — DRF-1126.

The defect this read exists for lives one repository over. The bot's
master-facing schedule screen (``apps/master_api/services/schedule.py``,
``build_schedule``) draws its working frame from the bot's own
``apps.scheduling`` ``WorkingHours``. That table lost its last
Ayla-syncing writer when DRF-1062 removed the invite-flow seeder, so the
salon edits the graph on its own surface, the customer's picker (reading
Ayla since PR #1186) shows the new hours, and the master's screen shows
the old ones. Neither side reports an error and both look right.

The bot could not close that on its own: its Ayla client has
``get_available_times`` (bookable slots for one service on one day) and
``create_specialist_time_off``, and nothing that answers "what is this
master's working frame". This is that missing read, and these tests pin
the properties the consumer is entitled to rely on:

* all seven weekdays come back, always, so an unset day is never a guess;
* the three frame sources arrive together, because resolving one day
  needs all three;
* an absence that merely *overlaps* the window is included;
* no personal data, ever — the frame, not the bookings;
* ``tenant_id`` is a claim, checked, and a foreign specialist is a 404.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.models import (
    Appointment, SpecialistScheduleException, SpecialistTimeOff,
    SpecialistWorkingHours,
)
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

TOKEN = "internal-token-1126"
MSK = ZoneInfo("Europe/Moscow")
MONDAY = date(2026, 8, 17)


def _url(specialist_id) -> str:
    return f"/api/v1/internal/specialists/{specialist_id}/schedule/"


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
    return Tenant.objects.create(slug="int-sch-a", name="Салон А")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="int-sch-b", name="Салон Б")


def _make_master(username, tenant, phone):
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
    return _make_master("sch_olga", salon, "+79991126001")


@pytest.fixture
def foreign_master(db, other_salon):
    return _make_master("sch_inna", other_salon, "+79991126002")


def _get(bearer, master, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return bearer.get(_url(master.id) + (f"?{query}" if query else ""))


@pytest.mark.django_db
class TestTheFrameComesBackWhole:

    def test_all_seven_weekdays_even_when_only_two_are_set(
        self, bearer, salon, master,
    ):
        """A missing row means "not working". A consumer handed a
        two-element list has to infer that, which is exactly the
        guesswork this endpoint exists to stop."""
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=0, is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
            break_start=time(13, 0), break_end=time(14, 0),
        )
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=1, is_working_day=True,
            start_time=time(12, 0), end_time=time(20, 0),
        )

        resp = _get(bearer, master, tenant_id=salon.id)

        assert resp.status_code == 200, resp.data
        weekly = resp.data["data"]["weekly"]
        assert [d["day_of_week"] for d in weekly] == list(range(7))
        assert weekly[0]["start_time"] == "10:00"
        assert weekly[0]["break_start"] == "13:00"
        assert weekly[1]["start_time"] == "12:00"
        # The five that were never configured say so out loud.
        for day in weekly[2:]:
            assert day["is_working_day"] is False
            assert day["start_time"] is None

    def test_the_three_frame_sources_arrive_together(
        self, bearer, salon, master,
    ):
        """Weekly template, one-off override and absence in one call.

        Resolving a single day needs all three: the exception replaces
        the weekly frame, the absence subtracts from whichever won.
        Splitting them over three round trips invites a consumer to draw
        the day from the template alone.
        """
        SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=MONDAY.weekday(),
            is_working_day=True,
            start_time=time(10, 0), end_time=time(19, 0),
        )
        SpecialistScheduleException.objects.create(
            specialist=master, date=MONDAY, is_working_day=True,
            start_time=time(12, 0), end_time=time(16, 0),
            note="короткий день",
        )
        SpecialistTimeOff.objects.create(
            specialist=master,
            start_at=datetime.combine(MONDAY, time(14, 0), tzinfo=MSK),
            end_at=datetime.combine(MONDAY, time(15, 0), tzinfo=MSK),
            reason="врач",
        )

        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
        )

        assert resp.status_code == 200, resp.data
        data = resp.data["data"]
        assert data["weekly"][MONDAY.weekday()]["start_time"] == "10:00"
        assert len(data["exceptions"]) == 1
        assert data["exceptions"][0]["date"] == MONDAY.isoformat()
        assert data["exceptions"][0]["start_time"] == "12:00"
        assert data["exceptions"][0]["note"] == "короткий день"
        assert len(data["time_off"]) == 1
        assert data["timezone"] == "Europe/Moscow"

    def test_an_absence_that_only_overlaps_the_window_is_included(
        self, bearer, salon, master,
    ):
        """Started yesterday, ends tomorrow — today is still blocked.

        A consumer sent only the blocks *starting* inside the window
        would draw today as free, which is the same "credible empty day"
        failure one layer up.
        """
        SpecialistTimeOff.objects.create(
            specialist=master,
            start_at=datetime.combine(
                MONDAY - timedelta(days=1), time(9, 0), tzinfo=MSK,
            ),
            end_at=datetime.combine(
                MONDAY + timedelta(days=1), time(9, 0), tzinfo=MSK,
            ),
            reason="болею",
        )

        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
        )

        assert resp.status_code == 200, resp.data
        assert len(resp.data["data"]["time_off"]) == 1

    def test_exceptions_outside_the_window_are_not_sent(
        self, bearer, salon, master,
    ):
        SpecialistScheduleException.objects.create(
            specialist=master, date=MONDAY + timedelta(days=30),
            is_working_day=False, note="отпуск",
        )

        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
        )

        assert resp.data["data"]["exceptions"] == []

    def test_the_default_window_is_a_fortnight_from_today(
        self, bearer, salon, master,
    ):
        from django.utils import timezone as dj_timezone

        resp = _get(bearer, master, tenant_id=salon.id)

        data = resp.data["data"]
        today = dj_timezone.localdate()
        assert data["from"] == today.isoformat()
        assert data["to"] == (today + timedelta(days=13)).isoformat()


@pytest.mark.django_db
class TestItIsTheFrameAndNotTheBookings:

    def test_no_appointment_and_no_customer_reaches_this_response(
        self, bearer, salon, master,
    ):
        """Who is coming is the other half of the master's screen and it
        already has a source — the ``RemoteBookingProxy`` mirror, which
        DRF-1085 pointed that screen at. Serving it here would create a
        second answer to a settled question and would put customers'
        data behind a shared service token.
        """
        category = ServiceCategory.objects.create(
            name="Кат 1126", slug="cat-1126",
        )
        service = Service.objects.create(
            specialist=master, category=category, name="Стрижка",
            price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        )
        customer = User.objects.create_user(
            username="sch_client", password="x", role="client",
            phone="+79991126003", first_name="Анна", last_name="К.",
        )
        start = datetime.combine(MONDAY, time(11, 0), tzinfo=MSK)
        Appointment.objects.create(
            client=customer, specialist=master, service=service,
            tenant=salon,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=60),
            price=service.price, status=Appointment.Status.CONFIRMED,
            snapshot_service_name=service.name,
            snapshot_price=service.price,
            snapshot_duration_minutes=60,
            idempotency_key=str(uuid4()),
        )

        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": MONDAY.isoformat(), "to": MONDAY.isoformat()},
        )

        assert resp.status_code == 200, resp.data
        serialised = str(resp.data)
        assert "Анна" not in serialised
        assert "79991126003" not in serialised
        assert "sch_client" not in serialised
        assert "appointment" not in serialised.lower()
        assert "bookings" not in serialised


@pytest.mark.django_db
class TestTenantIdIsAClaimAndNotACredential:

    def test_a_specialist_of_another_salon_is_404_not_403(
        self, bearer, salon, foreign_master,
    ):
        """403 would confirm the UUID is real to whoever guessed it.
        Same rule as the POST beside it (DRF-1036 adjacency)."""
        resp = _get(bearer, foreign_master, tenant_id=salon.id)

        assert resp.status_code == 404
        assert resp.data["error"]["code"] == "NOT_FOUND"

    def test_a_specialist_that_does_not_exist_is_the_same_404(
        self, bearer, salon,
    ):
        resp = bearer.get(f"{_url(uuid4())}?tenant_id={salon.id}")

        assert resp.status_code == 404
        assert resp.data["error"]["code"] == "NOT_FOUND"

    def test_no_bearer_no_schedule(self, salon, master):
        resp = APIClient().get(f"{_url(master.id)}?tenant_id={salon.id}")

        assert resp.status_code in (401, 403)

    def test_a_wrong_bearer_no_schedule(self, salon, master):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer not-the-token")

        resp = c.get(f"{_url(master.id)}?tenant_id={salon.id}")

        assert resp.status_code in (401, 403)

    def test_tenant_id_is_required(self, bearer, master):
        resp = bearer.get(_url(master.id))

        assert resp.status_code == 400
        assert resp.data["error"]["code"] == "VALIDATION_ERROR"

    def test_a_tenant_id_that_is_not_a_uuid_is_400_not_500(
        self, bearer, master,
    ):
        resp = _get(bearer, master, tenant_id="not-a-uuid")

        assert resp.status_code == 400


@pytest.mark.django_db
class TestTheWindowIsBounded:

    def test_a_reversed_range_is_refused(self, bearer, salon, master):
        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": "2026-08-20", "to": "2026-08-10"},
        )

        assert resp.status_code == 400

    def test_a_range_longer_than_sixty_days_is_refused(
        self, bearer, salon, master,
    ):
        """Capped rather than paginated: a frame is not a feed, and an
        uncapped range behind a shared token is an easy way to make the
        database do unbounded work on request."""
        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{"from": "2026-08-01", "to": "2026-12-01"},
        )

        assert resp.status_code == 400

    def test_exactly_sixty_days_is_allowed(self, bearer, salon, master):
        start = date(2026, 8, 1)
        resp = _get(
            bearer, master, tenant_id=salon.id,
            **{
                "from": start.isoformat(),
                "to": (start + timedelta(days=60)).isoformat(),
            },
        )

        assert resp.status_code == 200, resp.data

    def test_a_malformed_date_is_400_not_500(self, bearer, salon, master):
        resp = _get(
            bearer, master, tenant_id=salon.id, **{"from": "19-08-2026"},
        )

        assert resp.status_code == 400


@pytest.mark.django_db
class TestReadAndWriteCannotDrift:

    def test_the_exception_shape_matches_the_salon_admin_surface(
        self, salon, master,
    ):
        """``_exception_to_dict`` is copied here rather than imported, to
        keep this module small enough to read in one sitting. This is the
        pin that makes the copy safe: change either side and it fails."""
        from users.internal_schedule_api import (
            _exception_to_dict as internal_shape,
        )
        from users.schedule_admin_api import (
            _exception_to_dict as admin_shape,
        )

        row = SpecialistScheduleException.objects.create(
            specialist=master, date=MONDAY, is_working_day=True,
            start_time=time(12, 0), end_time=time(16, 0),
            break_start=time(13, 30), break_end=time(14, 0),
            note="короткий день",
        )

        assert internal_shape(row) == admin_shape(row)

    def test_the_weekly_shape_matches_the_pro_app_surface(
        self, bearer, salon, master,
    ):
        """The master's bot screen and the master's own pro-app tab must
        not disagree about an unset weekday."""
        from users.schedule_api import _wh_to_dict

        wh = SpecialistWorkingHours.objects.create(
            specialist=master, day_of_week=2, is_working_day=True,
            start_time=time(9, 0), end_time=time(18, 0),
        )

        resp = _get(bearer, master, tenant_id=salon.id)

        assert resp.data["data"]["weekly"][2] == _wh_to_dict(wh)
