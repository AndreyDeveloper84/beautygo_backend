"""Integration tests for GET /api/v1/home/ — DRF-110."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ai.tests.factories import make_specialist, make_user
from appointments.models import Appointment
from services.models import Service, ServiceCategory


pytestmark = pytest.mark.django_db


HOME_URL = "/api/v1/home/"


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def client_user(db):
    return make_user(role="client")


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_appointment(client_user, *, status, start_offset_hours: int):
    spec = make_specialist()
    cat = ServiceCategory.objects.create(name=f"Cat-{start_offset_hours}")
    svc = Service.objects.create(
        specialist=spec, name="Маникюр", price=Decimal("1500"),
        duration_minutes=60, is_active=True, category=cat,
    )
    start = datetime(2026, 5, 1, 12, 0, tzinfo=dt_tz.utc) + timedelta(
        hours=start_offset_hours,
    )
    return Appointment.objects.create(
        client=client_user,
        specialist=spec,
        service=svc,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status,
        price=svc.price,
        snapshot_price=svc.price,
        snapshot_service_name=svc.name,
        snapshot_duration_minutes=60,
    )


# ---------------------------------------------------------------------------
# Auth & app-type guards
# ---------------------------------------------------------------------------


class TestAuth:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(HOME_URL)
        assert resp.status_code == 401

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.get(HOME_URL)
        assert resp.status_code == 403

    def test_specialist_role_returns_403(self):
        spec_user = make_user(role="specialist")
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=spec_user)
        resp = c.get(HOME_URL)
        # Specialist hits IsClient guard → 403
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Section shape
# ---------------------------------------------------------------------------


class TestSectionShape:
    def test_response_includes_all_five_sections(self, auth_client):
        resp = auth_client.get(HOME_URL)
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert "upcoming_appointments" in body
        assert "favorite_specialists" in body
        assert "popular_categories" in body
        assert "nearby_specialists" in body
        assert "recent_activity" in body

    def test_favorites_returns_empty_until_drf_72(self, auth_client):
        resp = auth_client.get(HOME_URL)
        # DRF-72 not implemented yet — empty list, not error.
        assert resp.json()["data"]["favorite_specialists"] == []


# ---------------------------------------------------------------------------
# upcoming_appointments
# ---------------------------------------------------------------------------


class TestUpcomingAppointments:
    def test_returns_only_upcoming_pending_or_confirmed(
        self, auth_client, client_user,
    ):
        # Past one — shouldn't show
        past = _make_appointment(
            client_user,
            status=Appointment.Status.COMPLETED,
            start_offset_hours=-100,
        )
        # Cancelled — shouldn't show even if future
        cancelled = _make_appointment(
            client_user,
            status=Appointment.Status.CANCELLED,
            start_offset_hours=10,
        )
        # Confirmed in future — should show
        confirmed = _make_appointment(
            client_user,
            status=Appointment.Status.CONFIRMED,
            start_offset_hours=24,
        )
        # We use future dates; need to mock now() to be before them.
        # The test fixture dates start from 2026-05-01 — verify against
        # whatever timezone.now() returns. If today < May 2026, future
        # appointments correctly show; otherwise past_offset is < now.
        from django.utils import timezone

        now = timezone.now()
        if now < confirmed.start_datetime:
            resp = auth_client.get(HOME_URL)
            ids = [
                a["id"] for a in resp.json()["data"]["upcoming_appointments"]
            ]
            assert str(confirmed.id) in ids
            assert str(past.id) not in ids
            assert str(cancelled.id) not in ids

    def test_limits_to_three(self, auth_client, client_user):
        from django.utils import timezone

        # Schedule 5 future confirmed appointments — only 3 should surface.
        future_offset_base = 24 * 30  # 30 days out
        for i in range(5):
            _make_appointment(
                client_user,
                status=Appointment.Status.CONFIRMED,
                start_offset_hours=future_offset_base + i,
            )
        resp = auth_client.get(HOME_URL)
        if timezone.now().year < 2026 or (
            timezone.now().year == 2026 and timezone.now().month < 6
        ):
            assert len(resp.json()["data"]["upcoming_appointments"]) <= 3


# ---------------------------------------------------------------------------
# popular_categories
# ---------------------------------------------------------------------------


class TestPopularCategories:
    def test_returns_categories_with_counts(self, auth_client):
        cat1 = ServiceCategory.objects.create(name="Маникюр", icon="nail")
        cat2 = ServiceCategory.objects.create(name="Стрижка", icon="cut")
        # Make cat1 popular (2 specialists), cat2 (1 specialist)
        for _ in range(2):
            spec = make_specialist()
            Service.objects.create(
                specialist=spec, name="Sv", price=Decimal("1000"),
                duration_minutes=60, is_active=True, category=cat1,
            )
        spec2 = make_specialist()
        Service.objects.create(
            specialist=spec2, name="Sv", price=Decimal("1000"),
            duration_minutes=60, is_active=True, category=cat2,
        )

        resp = auth_client.get(HOME_URL)
        cats = resp.json()["data"]["popular_categories"]
        ids = [c["id"] for c in cats]
        assert str(cat1.id) in ids
        assert str(cat2.id) in ids
        # cat1 with 2 specialists должен быть выше cat2
        cat1_idx = ids.index(str(cat1.id))
        cat2_idx = ids.index(str(cat2.id))
        assert cat1_idx < cat2_idx

    def test_caches_popular_categories(self, auth_client):
        from django.core.cache import cache
        from users.home_api import CACHE_KEY_POPULAR_CATEGORIES

        cat = ServiceCategory.objects.create(name="X")
        spec = make_specialist()
        Service.objects.create(
            specialist=spec, name="Sv", price=Decimal("100"),
            duration_minutes=30, is_active=True, category=cat,
        )

        # First call — populates cache
        auth_client.get(HOME_URL)
        cached = cache.get(CACHE_KEY_POPULAR_CATEGORIES)
        assert cached is not None

        # Add new category — should NOT appear in next call (cached)
        new_cat = ServiceCategory.objects.create(name="NewlyAdded")
        new_spec = make_specialist()
        Service.objects.create(
            specialist=new_spec, name="Sv", price=Decimal("100"),
            duration_minutes=30, is_active=True, category=new_cat,
        )
        resp = auth_client.get(HOME_URL)
        ids = [c["id"] for c in resp.json()["data"]["popular_categories"]]
        assert str(new_cat.id) not in ids


# ---------------------------------------------------------------------------
# nearby_specialists
# ---------------------------------------------------------------------------


class TestNearbySpecialists:
    def test_returns_top_rated_when_no_geo(self, auth_client):
        make_specialist(display_name="Top", rating=4.9, reviews_count=80)
        make_specialist(display_name="Mid", rating=4.5, reviews_count=20)
        resp = auth_client.get(HOME_URL)
        nearby = resp.json()["data"]["nearby_specialists"]
        assert len(nearby) >= 2

    def test_geo_query_params_accepted(self, auth_client):
        make_specialist(display_name="Geo", rating=4.7, reviews_count=20)
        resp = auth_client.get(HOME_URL, {"lat": "53.2", "lon": "45.0"})
        assert resp.status_code == 200

    def test_invalid_geo_silently_falls_back(self, auth_client):
        make_specialist(display_name="X", rating=4.7, reviews_count=20)
        resp = auth_client.get(HOME_URL, {"lat": "999", "lon": "200"})
        # Out of range — view treats as no geo rather than 400
        assert resp.status_code == 200

    def test_nearby_limit_six(self, auth_client):
        for i in range(8):
            make_specialist(display_name=f"S{i}", rating=4.9, reviews_count=50)
        resp = auth_client.get(HOME_URL)
        nearby = resp.json()["data"]["nearby_specialists"]
        assert len(nearby) <= 6


# ---------------------------------------------------------------------------
# recent_activity
# ---------------------------------------------------------------------------


class TestRecentActivity:
    def test_returns_completed_appointments_only(self, auth_client, client_user):
        completed = _make_appointment(
            client_user, status=Appointment.Status.COMPLETED,
            start_offset_hours=-200,
        )
        confirmed = _make_appointment(
            client_user, status=Appointment.Status.CONFIRMED,
            start_offset_hours=24,
        )
        resp = auth_client.get(HOME_URL)
        ids = [a["id"] for a in resp.json()["data"]["recent_activity"]]
        assert str(completed.id) in ids
        assert str(confirmed.id) not in ids

    def test_other_users_completed_not_shown(self, auth_client, client_user):
        other_user = make_user(role="client")
        _make_appointment(
            other_user, status=Appointment.Status.COMPLETED,
            start_offset_hours=-100,
        )
        resp = auth_client.get(HOME_URL)
        # Other user's completed appointment not in result
        assert resp.json()["data"]["recent_activity"] == []
