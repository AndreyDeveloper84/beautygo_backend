"""S3A internal Bearer catalog read — SalonService + SpecialistService.

The Ayla bot (S3B) mirrors the new canonical catalog through these
Bearer-authed endpoints. Pins: auth boundary, stable ids, resolved
duration/health on the bookable, tenant filtering.
Spec: docs/CATALOG_DOMAIN_REBUILD_S3_DESIGN_2026-07.md.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-s3a"
SALON_URL = "/api/v1/internal/catalog/salon-services/"
SPEC_URL = "/api/v1/internal/catalog/specialist-services/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="s3a-t", name="S3A Tenant")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="S3A Маникюр", slug="s3a-man")


@pytest.fixture
def gated_template(category):
    return ServiceTemplate.objects.create(
        category=category, name="Гейтед шаблон", name_short="Гейт",
        duration_default=60, requires_health_check=True,
    )


@pytest.fixture
def salon_service(tenant, gated_template, category):
    return SalonService.objects.create(
        tenant=tenant, template=gated_template, category=category,
        name="Гейтед салон-услуга",
    )


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="s3a_spec", password="x", role="specialist",
        phone="+79995303100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "S3A Spec"
    p.yclients_staff_id = "9001"
    p.save()
    return p


@pytest.fixture
def specialist_service(salon_service, specialist):
    return SpecialistService.objects.create(
        salon_service=salon_service, specialist=specialist,
        duration_minutes=45, price=Decimal("1500"),
        requires_health_check=False,
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


@pytest.mark.django_db
class TestInternalCatalogAuth:
    def test_missing_bearer_denied(self, salon_service):
        assert _api(bearer=None).get(SALON_URL).status_code == 403

    def test_wrong_bearer_denied(self, salon_service):
        assert _api(bearer="nope").get(SALON_URL).status_code == 403

    def test_empty_token_fails_closed(self, settings, salon_service):
        settings.AYLA_INTERNAL_API_TOKEN = ""
        assert _api().get(SALON_URL).status_code == 403


@pytest.mark.django_db
class TestSalonServiceRead:
    def test_list_ok(self, salon_service):
        assert _api().get(SALON_URL).status_code == 200

    def test_detail_exposes_stable_id_and_template(self, salon_service, gated_template):
        r = _api().get(f"{SALON_URL}{salon_service.id}/")
        assert r.status_code == 200, r.data
        data = r.json().get("data", r.json())
        assert str(data["id"]) == str(salon_service.id)
        assert str(data["template"]) == str(gated_template.id)
        assert data["requires_health_check"] is False

    def test_filter_by_tenant(self, salon_service):
        other = Tenant.objects.create(slug="s3a-other", name="Other")
        r = _api().get(f"{SALON_URL}?tenant={other.id}")
        assert r.status_code == 200
        results = r.json().get("data", r.json())
        results = results.get("results", results) if isinstance(results, dict) else results
        assert all(str(x["tenant"]) != str(salon_service.tenant_id) for x in results)


@pytest.mark.django_db
class TestSpecialistServiceRead:
    def test_list_ok(self, specialist_service):
        assert _api().get(SPEC_URL).status_code == 200

    def test_detail_exposes_resolved_fields_and_stable_id(self, specialist_service, specialist):
        r = _api().get(f"{SPEC_URL}{specialist_service.id}/")
        assert r.status_code == 200, r.data
        data = r.json().get("data", r.json())
        assert str(data["id"]) == str(specialist_service.id)
        # own duration 45 wins the resolution cascade
        assert data["resolved_duration"] == 45
        # template floor requires health check -> resolved True even though
        # the specialist row has requires_health_check=False (D1 escalate-only)
        assert data["resolved_requires_health_check"] is True
        assert str(data["specialist"]) == str(specialist.id)
        assert data["yclients_staff_id"] == "9001"

    def test_filter_by_specialist(self, specialist_service, specialist):
        r = _api().get(f"{SPEC_URL}?specialist={specialist.id}")
        assert r.status_code == 200
        results = r.json().get("data", r.json())
        results = results.get("results", results) if isinstance(results, dict) else results
        assert len(results) == 1
        assert str(results[0]["id"]) == str(specialist_service.id)
