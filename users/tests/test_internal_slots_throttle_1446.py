"""DRF-1446 — the internal slots fan-out gets its own throttle bucket.

Why this exists. ``InternalSpecialistViewSet`` sets
``authentication_classes = []`` (the bot ships a Bearer service token,
not a JWT), so every call to it is *anonymous* as far as DRF is
concerned and was billed to the per-IP ``anon`` bucket — 30/min. Every
bot process reaches the backend from one source IP, so that single
bucket covered the whole fleet.

One schedule screen is a 14-day fan-out asked one date at a time, so
drawing it spent 14 of those 30. On the pilot (03.09, 07:56:16-07:56:49)
a user who looked at a second service spent the rest and the screen
429'd mid-draw — the booking path, which is the product.

The tests below pin four things:

1. the measured shape (a 14-request screen) is not throttled;
2. slots no longer spend the ``anon`` bucket — 31 calls pass, and an
   *unscoped* internal endpoint still answers afterwards;
3. the limiter is still a limiter (positive guard): at a deliberately
   tiny rate the 429 still arrives, so "no more 429s" can never be
   satisfied by simply removing the throttle;
4. external/public clients keep the protection they had — the public
   slots endpoint is untouched and still rides the default buckets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.throttling import (
    AnonRateThrottle,
    ScopedRateThrottle,
    SimpleRateThrottle,
    UserRateThrottle,
)

from appointments.models import SpecialistWorkingHours
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.internal_catalog_api import InternalSpecialistViewSet
from users.models import SpecialistProfile, User
from users.specialists_api import SpecialistViewSet

VALID_TOKEN = "test-ayla-internal-token-1446"
INTERNAL_URL = "/api/v1/internal/specialists/"
PUBLIC_URL = "/api/v1/specialists/"

# Measured on the pilot: the bot asks 14 consecutive dates, one request
# per date, to draw a single schedule screen (03.09 07:56:16 → 07:56:18,
# dates 2026-09-03 … 2026-09-16).
DATES_PER_SCREEN = 14

# The bucket the endpoint used to share with every other unscoped
# internal path. Exceeding it is exactly what broke booking.
OLD_ANON_LIMIT = 30


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture(autouse=True)
def _clean_throttle_history():
    """Throttle history lives in the cache; tests in one process would
    otherwise inherit each other's buckets."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="drf1446-t", name="DRF1446 Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="drf1446_spec", password="x", role="specialist",
        phone="+79996144601",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "DRF1446 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="DRF1446 Cat", slug="drf1446-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="DRF1446 Svc",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def horizon(specialist):
    """Every weekday a working day, so each date in the fan-out is a real
    request rather than a cheap empty answer."""
    for dow in range(7):
        SpecialistWorkingHours.objects.create(
            specialist=specialist, day_of_week=dow, is_working_day=True,
            start_time="09:00", end_time="18:00",
        )
    return (datetime.now(tz=timezone.utc) + timedelta(days=1)).date()


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    return c


def _slots(client, specialist, service, day):
    return client.get(
        f"{INTERNAL_URL}{specialist.id}/slots/"
        f"?service_id={service.id}&date={day.isoformat()}"
    )


def _set_rate(monkeypatch, scope, rate):
    """Lower one bucket for the duration of a test.

    Overriding ``settings.REST_FRAMEWORK`` does NOT work here:
    ``SimpleRateThrottle.THROTTLE_RATES`` is bound to the settings dict
    once, at import time (rest_framework/throttling.py:66), so a later
    settings swap leaves every throttle class still reading the original
    mapping. Patch the mapping the throttles actually consult.
    """
    monkeypatch.setitem(SimpleRateThrottle.THROTTLE_RATES, scope, rate)


@pytest.mark.django_db
class TestSlotsHaveTheirOwnBucket:
    def test_one_schedule_screen_is_not_throttled(
        self, specialist, service, horizon,
    ):
        """The measured shape: 14 dates, one request each."""
        client = _api()
        for i in range(DATES_PER_SCREEN):
            r = _slots(client, specialist, service, horizon + timedelta(days=i))
            assert r.status_code != 429, (
                f"schedule screen 429'd on date #{i + 1} of {DATES_PER_SCREEN}"
            )

    def test_slots_no_longer_spend_the_anon_bucket(
        self, specialist, service, horizon,
    ):
        """The regression. On the old code request #31 is a 429, because
        slots rode the shared 30/min ``anon`` bucket.

        Then: an internal endpoint that is STILL unscoped must answer
        afterwards — proving the fan-out drained its own bucket and not
        the shared one.
        """
        client = _api()
        for i in range(OLD_ANON_LIMIT + 1):
            day = horizon + timedelta(days=i % DATES_PER_SCREEN)
            r = _slots(client, specialist, service, day)
            assert r.status_code != 429, (
                f"slots throttled on request #{i + 1}; the endpoint is still "
                f"sharing the anon bucket ({OLD_ANON_LIMIT}/min)"
            )

        neighbour = client.get(f"{INTERNAL_URL}?tenant={specialist.tenant_id}")
        assert neighbour.status_code != 429, (
            "a full slots fan-out drained the anon bucket that the rest of "
            "/api/v1/internal/ still shares"
        )

    def test_limit_is_still_enforced(
        self, specialist, service, horizon, monkeypatch,
    ):
        """Positive guard. Without this, 'the 429s stopped' would also be
        true of a throttle that was simply deleted."""
        _set_rate(monkeypatch, "slots_internal", "3/min")
        client = _api()
        for i in range(3):
            r = _slots(client, specialist, service, horizon + timedelta(days=i))
            assert r.status_code != 429, f"429 came early, on request #{i + 1}"

        r = _slots(client, specialist, service, horizon + timedelta(days=3))
        assert r.status_code == 429, (
            "the 4th request past a 3/min cap must still be refused"
        )

    def test_scope_is_wired_and_named(self):
        """Mutation guard on the wiring itself: the scope must exist in
        settings and must be the one the view asks for."""
        from django.conf import settings as dj_settings

        rates = dj_settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        assert "slots_internal" in rates, (
            "DEFAULT_THROTTLE_RATES lost 'slots_internal'; ScopedRateThrottle "
            "would raise ImproperlyConfigured on every slots call"
        )
        assert InternalSpecialistViewSet.throttle_scope == "slots_internal"

        view = InternalSpecialistViewSet()
        view.action = "slots"
        assert [type(t) for t in view.get_throttles()] == [ScopedRateThrottle]


@pytest.mark.django_db
class TestExternalProtectionUnchanged:
    def test_public_slots_keep_the_default_buckets(self):
        """The new internal quota must not relax anything for public
        clients: the public viewset still rides anon + user."""
        view = SpecialistViewSet()
        view.action = "slots"
        assert [type(t) for t in view.get_throttles()] == [
            AnonRateThrottle, UserRateThrottle,
        ]

    def test_public_slots_still_throttled_for_a_real_client(
        self, specialist, service, horizon, monkeypatch,
    ):
        """Behavioural half: a signed-in app client hitting the PUBLIC
        endpoint is still cut off at its own cap."""
        _set_rate(monkeypatch, "user", "3/min")
        viewer = User.objects.create_user(
            username="drf1446_viewer", password="x", role="client",
            phone="+79996144609",
        )
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=viewer)
        url = (
            f"{PUBLIC_URL}{specialist.id}/slots/"
            f"?service_id={service.id}&date={horizon.isoformat()}"
        )
        for i in range(3):
            assert c.get(url).status_code != 429, f"429 early on #{i + 1}"
        assert c.get(url).status_code == 429, (
            "public slots lost their user-rate protection"
        )

    def test_other_internal_paths_still_ride_the_shared_bucket(self):
        """Scope discipline: this ticket moved ONE action. If a later
        change moves more, that is a deliberate decision, not a
        side-effect — this test makes it visible."""
        view = InternalSpecialistViewSet()
        for action in ("list", "retrieve", "services"):
            view.action = action
            assert [type(t) for t in view.get_throttles()] == [
                AnonRateThrottle, UserRateThrottle,
            ], f"action '{action}' unexpectedly changed buckets"
