"""S3D — internal catalog contract conformance (#1044).

Pins the FROZEN wire contract of the internal catalog mirror
(`docs/CATALOG_INTERNAL_API_CONTRACT.md`) as literals, S0-C style
(cf. appointments/tests/test_emitter_conformance_196.py): the bot S3B mirror
hard-reads these paths / methods / field names, so any silent Ayla-side drift
(renamed / added / removed field, changed path, a write verb slipping onto the
read mirror) must trip THIS test rather than break the bot in production.

If a change here is intentional, bump the contract doc + notify S3B, then update
these literals in the same PR.
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

VALID_TOKEN = "test-ayla-internal-token-s3d"
CATALOG_BASE = "/api/v1/internal/catalog/"
SALON_URL = f"{CATALOG_BASE}salon-services/"
SPEC_URL = f"{CATALOG_BASE}specialist-services/"

# --- FROZEN field sets (mirror of CATALOG_INTERNAL_API_CONTRACT.md) ---------- #
SALON_SERVICE_FIELDS = {
    "id", "tenant", "template", "category", "name",
    "duration_minutes", "base_price", "requires_health_check",
    "is_active", "source",
    # DRF-1308 (additive, 2026-08-23): цели, разрешённые по дереву
    # категорий на стороне Ayla. Расширение контракта осознанное —
    # у бота нет таблицы категорий, разрешать дерево он не может.
    "goals",
    "created_at", "updated_at",
}
SPECIALIST_SERVICE_FIELDS = {
    "id", "salon_service", "specialist", "user_id", "tenant", "template",
    # C6 link keys (additive 2026-07-19, orchestrator decision) — the bot
    # matches (category_slug, normalized name) + duration tiebreaker.
    "name", "category_slug",
    "duration_minutes", "resolved_duration",
    "requires_health_check", "resolved_requires_health_check",
    "price", "buffer_after_minutes", "is_active",
    "yclients_staff_id", "reviews_count", "rating",
    "created_at", "updated_at",
}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="s3d-t", name="S3D Tenant")


@pytest.fixture
def template(db):
    cat = ServiceCategory.objects.create(name="S3D Cat", slug="s3d-cat")
    return ServiceTemplate.objects.create(
        category=cat, name="S3D Tpl", name_short="S3D",
        duration_default=60, requires_health_check=True,
    )


@pytest.fixture
def salon_service(tenant, template):
    return SalonService.objects.create(
        tenant=tenant, template=template, category=template.category,
        name="S3D Salon Service",
    )


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="s3d_spec", password="x", role="specialist",
        phone="+79995606100",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.yclients_staff_id = "7001"
    p.reviews_count = 9
    p.rating = Decimal("4.5")
    p.save()
    return p


@pytest.fixture
def specialist_service(salon_service, specialist):
    return SpecialistService.objects.create(
        salon_service=salon_service, specialist=specialist,
        duration_minutes=45, price=Decimal("1500"),
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _detail(body):
    return body.get("data", body) if isinstance(body, dict) else body


# --------------------------------------------------------------------------- #
# Paths + methods (read-only mirror)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestCatalogContractPathsAndMethods:
    @pytest.mark.parametrize("url", [SALON_URL, SPEC_URL])
    def test_get_list_ok(self, url, salon_service, specialist_service):
        assert _api().get(url).status_code == 200

    @pytest.mark.parametrize("url", [SALON_URL, SPEC_URL])
    def test_write_verbs_rejected(self, url, salon_service, specialist_service):
        # Read-only mirror — POST/PUT/PATCH/DELETE must not be routed to a
        # write handler (405 Method Not Allowed).
        assert _api().post(url, {}, format="json").status_code == 405
        assert _api().delete(url).status_code == 405

    @pytest.mark.parametrize("url", [SALON_URL, SPEC_URL])
    def test_auth_boundary(self, url, salon_service, specialist_service):
        assert _api(bearer=None).get(url).status_code == 403
        assert _api(bearer="wrong").get(url).status_code == 403


# --------------------------------------------------------------------------- #
# Frozen field sets
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestCatalogContractFieldSets:
    def test_salon_service_field_set(self, salon_service):
        r = _api().get(f"{SALON_URL}{salon_service.id}/")
        assert r.status_code == 200, r.data
        assert set(_detail(r.json()).keys()) == SALON_SERVICE_FIELDS

    def test_specialist_service_field_set(self, specialist_service):
        r = _api().get(f"{SPEC_URL}{specialist_service.id}/")
        assert r.status_code == 200, r.data
        assert set(_detail(r.json()).keys()) == SPECIALIST_SERVICE_FIELDS


# --------------------------------------------------------------------------- #
# Stable-id semantics (the fact that bit us — user_id != specialist)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestCatalogContractStableIds:
    def test_specialist_service_id_semantics(self, specialist_service, specialist):
        data = _detail(_api().get(f"{SPEC_URL}{specialist_service.id}/").json())
        # booking key
        assert str(data["id"]) == str(specialist_service.id)
        # specialist = SpecialistProfile.id ; user_id = canonical User.id ; distinct
        assert str(data["specialist"]) == str(specialist.id)
        assert str(data["user_id"]) == str(specialist.user_id)
        assert str(data["user_id"]) != str(data["specialist"])
        # discovery key present
        assert str(data["template"]) == str(specialist_service.salon_service.template_id)
