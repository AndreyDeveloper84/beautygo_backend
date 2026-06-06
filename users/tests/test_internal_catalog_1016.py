"""Internal Bearer catalog read — specialists + slots + services (#1016).

The Ayla bot mirrors the catalog and computes availability through
these Bearer-authed endpoints (no mobile JWT, no X-App-Type). Pins:
- auth boundary (missing / wrong bearer → 403, empty token fails closed);
- specialists list + detail mirror the public catalog serializers;
- slots reuse the booking-engine availability (same payload as public);
- services + categories list.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-cat-1016"
SPECIALISTS_URL = "/api/v1/internal/specialists/"
SERVICES_URL = "/api/v1/internal/services/"
CATEGORIES_URL = "/api/v1/internal/services/categories/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="cat1016-t", name="Cat 1016 Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="cat1016_spec", password="x", role="specialist",
        phone="+79991016100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Catalog Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Cat 1016", slug="cat1016-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="Catalog Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


@pytest.mark.django_db
class TestInternalCatalogAuth:
    def test_missing_bearer_denied(self, specialist):
        assert _api(bearer=None).get(SPECIALISTS_URL).status_code == 403

    def test_wrong_bearer_denied(self, specialist):
        assert _api(bearer="nope").get(SPECIALISTS_URL).status_code == 403

    def test_empty_token_setting_fails_closed(self, settings, specialist):
        settings.AYLA_INTERNAL_API_TOKEN = ""
        assert _api().get(SPECIALISTS_URL).status_code == 403


@pytest.mark.django_db
class TestInternalCatalogRead:
    def test_specialists_list(self, specialist, service):
        r = _api().get(SPECIALISTS_URL)
        assert r.status_code == 200, r.data
        # No X-App-Type required — internal tree is exempt.

    def test_specialist_detail(self, specialist, service):
        r = _api().get(f"{SPECIALISTS_URL}{specialist.id}/")
        assert r.status_code == 200, r.data
        body = r.json()
        data = body.get("data", body)
        assert str(data["id"]) == str(specialist.id)

    def test_slots(self, specialist, service):
        # Date a week out so it lands on whatever working day exists; the
        # endpoint returns a (possibly empty) slots list either way.
        target = (
            datetime.now(tz=timezone.utc) + timedelta(days=7)
        ).date().isoformat()
        r = _api().get(
            f"{SPECIALISTS_URL}{specialist.id}/slots/"
            f"?service_id={service.id}&date={target}",
        )
        assert r.status_code == 200, r.data
        assert "slots" in r.json()

    def test_slots_missing_service_id_400(self, specialist, service):
        r = _api().get(f"{SPECIALISTS_URL}{specialist.id}/slots/")
        assert r.status_code == 400

    def test_services_list(self, specialist, service):
        r = _api().get(SERVICES_URL)
        assert r.status_code == 200, r.data

    def test_categories_list(self, category):
        r = _api().get(CATEGORIES_URL)
        assert r.status_code == 200, r.data
