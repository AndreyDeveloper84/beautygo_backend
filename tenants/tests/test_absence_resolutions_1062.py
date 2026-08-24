"""Absence with resolutions — DRF-1062 §C.

The dead end this removes: today an administrator marking a master ill
gets 409 and nothing else, exactly when the feature is needed. The
replacement is not "drop the 409" — that would strand booked clients
instead of the salon. It is: preview what breaks, decide per booking,
then apply both together or neither.

Covered here:
- the preview lists displaced bookings and prints local time, not UTC;
- an absence with decisions cancels those bookings and blocks the time;
- a booking made between preview and confirm invalidates the token;
- leaving a booking undecided still refuses;
- the cancellation carries the vocabulary the bot needs to offer a new
  slot for the same service.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import (
    Appointment,
    OutboxEvent,
    SpecialistTimeOff,
)
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


def _make_user(*, username, role="client", phone=""):
    return User.objects.create_user(
        username=username, password="x", role=role, phone=phone,
    )


def _client_as(user, tenant) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    c.defaults["HTTP_X_TENANT"] = tenant.slug
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="abs-1062", name="Салон отсутствий")


@pytest.fixture
def admin(db, salon):
    user = _make_user(username="abs_admin", role="admin", phone="+79992062001")
    TenantUserRelationship.objects.filter(user=user).delete()
    TenantUserRelationship.objects.create(
        user=user, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return user


@pytest.fixture
def master(db, salon):
    user = _make_user(username="abs_master", role="specialist", phone="+79992062002")
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.display_name = "Мастер"
    profile.timezone = "Europe/Moscow"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def service(db, master):
    category = ServiceCategory.objects.create(name="Абс", slug="abs-1062")
    return Service.objects.create(
        specialist=master, category=category, name="УЗ-кавитация",
        price="1000.00", duration_minutes=60, is_active=True,
    )


@pytest.fixture
def sick_day(db):
    """A window three days out — clear of the 60-minute lead time."""
    start = (timezone.now() + timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0,
    )
    return start, start + timedelta(hours=10)


# Client phone numbers are ALLOCATED, never derived from ``hash()``.
#
# DRF-1364. This file used to number its clients
# ``f"+7999206{hash(username) % 10000:04d}"``. Two things were wrong with
# that, and together they made the whole suite a lottery:
#
# * ``hash()`` of a str is salted per interpreter process — PYTHONHASHSEED
#   is unset in CI (.github/workflows/ci.yml, the test step sets no seed),
#   so the numbers this file inserted were different on every run;
# * the band it drew from, ``+7999206xxxx``, CONTAINS the staff fixtures:
#   the admin is +79992062001 and the master is +79992062002. A draw of
#   2001 or 2002 hits ``users_user_phone_key`` and the test dies in setup,
#   before it asserts anything.
#
# On 2026-08-24 the seed landed: run 32708650133 mapped "abs_r2" onto 2002
# and turned dev red on
# ``test_a_booking_made_while_deciding_invalidates_the_token``. Nothing
# about absences had changed — the same test fails identically on
# a734068, the last green commit, under PYTHONHASHSEED=1192.
#
# The replacement is unique BY CONSTRUCTION rather than by luck: a
# sequential allocation, in a band no other fixture in this file uses.
# There is no modulo left to collide on, so no future author has to know
# which four digits the staff already hold.
_CLIENT_PHONE_BAND = "+7999207"  # staff sit in +7999206xxxx — keep them apart
_allocated_client_phones: dict[str, str] = {}


def _client_phone(username: str) -> str:
    """A distinct phone per client username, stable within a run."""
    if username not in _allocated_client_phones:
        _allocated_client_phones[username] = (
            f"{_CLIENT_PHONE_BAND}{len(_allocated_client_phones):04d}"
        )
    return _allocated_client_phones[username]


def _booking(master, service, at, client_username):
    client = _make_user(username=client_username, phone=_client_phone(client_username))
    return Appointment.objects.create(
        client=client, specialist=master, service=service,
        start_datetime=at, end_datetime=at + timedelta(minutes=60),
        price=service.price, status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name, snapshot_price=service.price,
        snapshot_duration_minutes=60,
    )


def _impact_url(master, start, end) -> str:
    # Percent-encoded, as a correct client would send it: an unescaped
    # '+03:00' offset decodes to a space and is not a timestamp.
    query = urlencode({"start_at": start.isoformat(), "end_at": end.isoformat()})
    return f"/api/v1/tenants/me/masters/{master.id}/schedule/impact/?{query}"


def _time_off_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/time-off/"


class TestImpactPreview:
    def test_lists_the_bookings_the_absence_would_displace(
        self, admin, master, service, salon, sick_day,
    ):
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_c1")
        _booking(master, service, start + timedelta(hours=4), "abs_c2")

        resp = _client_as(admin, salon).get(_impact_url(master, start, end))

        assert resp.status_code == 200
        data = resp.data["data"]
        assert len(data["bookings"]) == 2
        assert data["impact_token"]
        assert data["bookings"][0]["service_name"] == "УЗ-кавитация"
        assert data["bookings"][0]["refund_percent_if_cancelled"] == 100.0

    def test_prints_local_time_not_utc(
        self, admin, master, service, salon, sick_day,
    ):
        """DRF-1071 found the records list showing UTC — 14:00 MSK read as
        11:00. An operator choosing whose booking to cancel must not be
        handed that."""
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_tz")

        resp = _client_as(admin, salon).get(_impact_url(master, start, end))

        booking = resp.data["data"]["bookings"][0]
        assert resp.data["data"]["timezone"] == "Europe/Moscow"
        assert booking["start_at_local"].endswith("+03:00")

    def test_preview_changes_nothing(self, admin, master, service, salon, sick_day):
        start, end = sick_day
        appointment = _booking(master, service, start + timedelta(hours=2), "abs_ro")

        _client_as(admin, salon).get(_impact_url(master, start, end))

        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.CONFIRMED
        assert not SpecialistTimeOff.objects.exists()

    def test_empty_window_returns_a_token_too(
        self, admin, master, salon, sick_day,
    ):
        start, end = sick_day

        resp = _client_as(admin, salon).get(_impact_url(master, start, end))

        assert resp.status_code == 200
        assert resp.data["data"]["bookings"] == []
        assert resp.data["data"]["impact_token"]

    def test_window_is_required(self, admin, master, salon):
        resp = _client_as(admin, salon).get(
            f"/api/v1/tenants/me/masters/{master.id}/schedule/impact/"
        )

        assert resp.status_code == 400


class TestAbsenceWithResolutions:
    def _confirm(self, admin, master, salon, start, end, token, bookings, reason="болезнь"):
        return _client_as(admin, salon).post(
            _time_off_url(master),
            {
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "reason": reason,
                "impact_token": token,
                "resolutions": [
                    {"appointment_id": b["appointment_id"], "action": "cancel"}
                    for b in bookings
                ],
            },
            format="json",
        )

    def test_sick_day_cancels_bookings_and_blocks_the_time(
        self, admin, master, service, salon, sick_day,
    ):
        start, end = sick_day
        a1 = _booking(master, service, start + timedelta(hours=2), "abs_x1")
        a2 = _booking(master, service, start + timedelta(hours=4), "abs_x2")
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        resp = self._confirm(
            admin, master, salon, start, end,
            preview["impact_token"], preview["bookings"],
        )

        assert resp.status_code == 201
        assert resp.data["data"]["cancelled_count"] == 2
        a1.refresh_from_db()
        a2.refresh_from_db()
        assert a1.status == Appointment.Status.CANCELLED
        assert a2.status == Appointment.Status.CANCELLED
        assert SpecialistTimeOff.objects.filter(specialist=master).count() == 1

    def test_absence_over_empty_time_needs_no_resolutions(
        self, admin, master, salon, sick_day,
    ):
        start, end = sick_day
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        resp = self._confirm(admin, master, salon, start, end, preview["impact_token"], [])

        assert resp.status_code == 201
        assert SpecialistTimeOff.objects.filter(specialist=master).count() == 1

    def test_a_booking_made_while_deciding_invalidates_the_token(
        self, admin, master, service, salon, sick_day,
    ):
        """The race this token exists for: on 2026-08-14 a client created
        two bookings inside one minute."""
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_r1")
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        _booking(master, service, start + timedelta(hours=6), "abs_r2")

        resp = self._confirm(
            admin, master, salon, start, end,
            preview["impact_token"], preview["bookings"],
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "IMPACT_CHANGED"
        # A fresh preview comes back so the administrator can re-decide.
        assert len(resp.data["error"]["details"]["bookings"]) == 2
        assert not SpecialistTimeOff.objects.exists()

    def test_leaving_a_booking_undecided_still_refuses(
        self, admin, master, service, salon, sick_day,
    ):
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_u1")
        _booking(master, service, start + timedelta(hours=4), "abs_u2")
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        resp = self._confirm(
            admin, master, salon, start, end,
            preview["impact_token"], preview["bookings"][:1],
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"
        assert len(resp.data["error"]["details"]["unresolved"]) == 1
        assert not SpecialistTimeOff.objects.exists()
        assert Appointment.objects.filter(
            status=Appointment.Status.CONFIRMED,
        ).count() == 2, "nothing may be cancelled when the call is refused"

    def test_unsupported_action_is_rejected(
        self, admin, master, service, salon, sick_day,
    ):
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_re")
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        resp = _client_as(admin, salon).post(
            _time_off_url(master),
            {
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "impact_token": preview["impact_token"],
                "resolutions": [{
                    "appointment_id": preview["bookings"][0]["appointment_id"],
                    "action": "reassign",
                }],
            },
            format="json",
        )

        assert resp.status_code == 400

    def test_plain_post_without_resolutions_keeps_the_old_409(
        self, admin, master, service, salon, sick_day,
    ):
        """The pro-app contract is untouched: no resolutions, no change."""
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_old")

        resp = _client_as(admin, salon).post(
            _time_off_url(master),
            {"start_at": start.isoformat(), "end_at": end.isoformat()},
            format="json",
        )

        assert resp.status_code == 409
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"


class TestCancellationVocabularyForTheBot:
    def test_event_says_master_unavailable_and_names_the_service(
        self, admin, master, service, salon, sick_day,
    ):
        """What the bot needs to offer another slot for the SAME service
        instead of restarting the funnel — and the reason_code that picks
        the 'салон обещает связаться' template for the client."""
        start, end = sick_day
        _booking(master, service, start + timedelta(hours=2), "abs_v1")
        preview = _client_as(admin, salon).get(_impact_url(master, start, end)).data["data"]

        _client_as(admin, salon).post(
            _time_off_url(master),
            {
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "impact_token": preview["impact_token"],
                "resolutions": [{
                    "appointment_id": preview["bookings"][0]["appointment_id"],
                    "action": "cancel",
                }],
            },
            format="json",
        )

        event = OutboxEvent.objects.filter(topic="booking.cancelled").latest("created_at")
        data = event.payload["data"]  # payload is the envelope; domain fields sit in .data

        assert data["reason_code"] == "master_unavailable"
        assert data["reason"] == "specialist_departure"
        assert data["refund_percent"] == 100.0
        assert data["service_name"] == "УЗ-кавитация"
        assert data["service_id"] == str(service.id)
        assert data["duration_minutes"] == 60
        assert data["specialist_id"] == str(master.id)
