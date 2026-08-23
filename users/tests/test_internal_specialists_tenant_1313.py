"""``?tenant=`` on the internal specialists list (DRF-1313).

The failure being pinned: ``GET /api/v1/internal/specialists/`` was
tenant-blind, so the bot's catalog mirror pulled the full active roster of the
platform once per syncing tenant and the first tenant to sync claimed every
master. On 2026-08-23 the five masters of four salons all landed under
``mkt-spatrium`` and three of five pilot salons could not be booked at all.

What these tests hold:

* the list narrows to the named tenant, and to *only* that tenant;
* every row carries its own ``tenant``, so the consumer can verify the scope
  it asked for actually held rather than trusting the query param;
* a malformed tenant is a 400 — not a silently ignored filter answering with
  the whole platform (that silence is the whole defect);
* omitting the param still works (the consumer deploys after this change) but
  logs a WARNING;
* nothing about the auth boundary moved, and the public Client App catalog did
  not gain the filter or the field.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-1313"
SPECIALISTS_URL = "/api/v1/internal/specialists/"
PUBLIC_SPECIALISTS_URL = "/api/v1/specialists/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="drf1313-a", name="Salon A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="drf1313-b", name="Salon B")


def _make_specialist(*, tenant, username, phone, name):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


@pytest.fixture
def spec_a(tenant_a):
    return _make_specialist(
        tenant=tenant_a, username="drf1313_a", phone="+79991313001",
        name="Salon A · massage",
    )


@pytest.fixture
def spec_b(tenant_b):
    return _make_specialist(
        tenant=tenant_b, username="drf1313_b", phone="+79991313002",
        name="Salon B · laser",
    )


@pytest.fixture
def spec_no_tenant(db):
    return _make_specialist(
        tenant=None, username="drf1313_none", phone="+79991313003",
        name="Unassigned master",
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    client = APIClient()
    if bearer is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return client


def _rows(response) -> list[dict]:
    """Unwrap the response envelope + DRF pagination into a row list."""
    body = response.json()
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    return payload


@pytest.mark.django_db
class TestTenantFilter:
    def test_unfiltered_list_still_returns_every_tenant(self, spec_a, spec_b):
        """The pre-fix behaviour, pinned deliberately.

        The param is optional so the consumer can deploy after this change;
        this test is what makes that choice explicit rather than accidental.
        If someone later makes ``tenant`` required, this test fails and the
        deploy-order consequence gets discussed instead of discovered.
        """
        ids = {row["id"] for row in _rows(_api().get(SPECIALISTS_URL))}
        assert {str(spec_a.id), str(spec_b.id)} <= ids

    def test_filter_returns_only_the_named_tenant(self, spec_a, spec_b):
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}"))
        assert [row["id"] for row in rows] == [str(spec_a.id)]

    def test_filter_excludes_the_other_tenant(self, spec_a, spec_b):
        """The exact August 23 symptom: Afrodita's master shown as SPAtrium's."""
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?tenant={spec_b.tenant_id}"))
        assert [row["id"] for row in rows] == [str(spec_b.id)]
        assert str(spec_a.id) not in {row["id"] for row in rows}

    def test_tenant_with_no_specialists_returns_empty(self, spec_a, tenant_b):
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?tenant={tenant_b.id}"))
        assert rows == []

    def test_unknown_tenant_uuid_returns_empty_not_everything(self, spec_a):
        """A well-formed but unknown tenant must return nothing.

        The dangerous failure mode is "filter matched no one, so return the
        unfiltered queryset" — that is what a mis-provisioned tenant id would
        hit, and it would silently re-create the defect.
        """
        rows = _rows(
            _api().get(f"{SPECIALISTS_URL}?tenant=00000000-0000-0000-0000-000000000000")
        )
        assert rows == []

    def test_malformed_tenant_is_400_not_ignored(self, spec_a, spec_b):
        response = _api().get(f"{SPECIALISTS_URL}?tenant=not-a-uuid")
        assert response.status_code == 400, response.data


@pytest.mark.django_db
class TestTenantInPayload:
    def test_list_row_carries_its_own_tenant(self, spec_a):
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}"))
        assert str(rows[0]["tenant"]) == str(spec_a.tenant_id)

    def test_detail_carries_tenant(self, spec_a):
        response = _api().get(f"{SPECIALISTS_URL}{spec_a.id}/")
        assert response.status_code == 200, response.data
        body = response.json()
        data = body.get("data", body)
        assert str(data["tenant"]) == str(spec_a.tenant_id)


@pytest.mark.no_auto_tenant
@pytest.mark.django_db
class TestTenantlessSpecialist:
    """``SpecialistProfile.tenant`` is nullable (``null=True`` until the
    DRF-242.4 backfill lands everywhere), so the NULL case is real and needs
    pinning in both directions.

    ``no_auto_tenant`` opts out of the root conftest's autouse pre_save signal,
    which otherwise stamps a default tenant onto any NULL-tenant profile — the
    exact precondition under test.
    """

    def test_tenantless_specialist_is_not_handed_to_a_salon(
        self, spec_a, spec_no_tenant,
    ):
        """A master belonging to no salon must not be handed to one.

        The filter is an equality match, so it drops out. Pinned because the
        alternative — leaking unassigned masters into whichever salon happened
        to ask — is the same class of bug this issue is about.
        """
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}"))
        assert [row["id"] for row in rows] == [str(spec_a.id)]

    def test_tenantless_specialist_serializes_tenant_as_null(
        self, spec_no_tenant,
    ):
        """NULL must reach the consumer as ``null``, not as an absent key or a
        borrowed id. The bot's cross-tenant guard reads this field; a wrong
        value there is worse than no value.
        """
        response = _api().get(f"{SPECIALISTS_URL}{spec_no_tenant.id}/")
        assert response.status_code == 200, response.data
        body = response.json()
        data = body.get("data", body)
        assert data["tenant"] is None


@pytest.mark.django_db
class TestNothingElseMoved:
    def test_missing_bearer_still_denied(self, spec_a):
        assert _api(bearer=None).get(SPECIALISTS_URL).status_code == 403

    def test_wrong_bearer_still_denied(self, spec_a):
        assert _api(bearer="nope").get(SPECIALISTS_URL).status_code == 403

    def test_bearer_still_required_when_a_tenant_is_named(self, spec_a):
        """Naming a tenant is not a credential. The filter narrows a list the
        caller was already authorized to read; it must never be a way in.
        """
        response = _api(bearer=None).get(
            f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}",
        )
        assert response.status_code == 403

    def test_public_catalog_did_not_gain_the_tenant_field(self, spec_a):
        """The Client App payload must not change shape.

        The public catalog is unauthenticated-adjacent and cross-salon by
        design; adding a tenant id there is a product decision nobody made.
        """
        from users.specialists_api import SpecialistListSerializer

        assert "tenant" not in SpecialistListSerializer.Meta.fields

    def test_public_catalog_did_not_gain_the_tenant_filter(self):
        from users.specialists_api import SpecialistFilter

        assert "tenant" not in SpecialistFilter.base_filters


@pytest.fixture
def viewset_logs(caplog):
    """Capture records from ``users.internal_catalog_api``.

    ``settings.LOGGING`` declares the ``users`` logger with
    ``propagate: False``, so records never reach the root logger and caplog's
    root handler cannot see them. Attaching caplog's own handler to the logger
    under test is narrower than flipping ``propagate``, which would change the
    behaviour of every logger in the ``users`` tree for the test's duration.
    """
    target = logging.getLogger("users.internal_catalog_api")
    previous_level = target.level
    target.addHandler(caplog.handler)
    target.setLevel(logging.WARNING)
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)
        target.setLevel(previous_level)


@pytest.mark.django_db
class TestTenantlessPullIsLoud:
    def test_list_without_tenant_logs_a_warning(self, spec_a, viewset_logs):
        assert _api().get(SPECIALISTS_URL).status_code == 200
        assert any(
            "internal.specialists.list_without_tenant" in record.getMessage()
            for record in viewset_logs.records
        ), "a tenant-blind pull must not be silent — that silence is the defect"

    def test_list_with_tenant_does_not_warn(self, spec_a, viewset_logs):
        response = _api().get(f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}")
        assert response.status_code == 200
        assert not any(
            "internal.specialists.list_without_tenant" in record.getMessage()
            for record in viewset_logs.records
        )


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="DRF-1313", slug="drf1313-cat")


@pytest.fixture
def service_a(spec_a, category):
    return Service.objects.create(
        specialist=spec_a, category=category, name="A service",
        price=Decimal("1000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.mark.django_db
class TestExistingFiltersStillWork:
    def test_is_available_filter_survives_the_subclass(self, spec_a, service_a):
        """``InternalSpecialistFilter`` subclasses the public filterset; the
        inherited filters must still be wired, not shadowed by the new one.
        """
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?is_available=true"))
        assert str(spec_a.id) in {row["id"] for row in rows}

    def test_service_id_filter_survives_the_subclass(self, spec_a, service_a):
        rows = _rows(_api().get(f"{SPECIALISTS_URL}?service_id={service_a.id}"))
        assert [row["id"] for row in rows] == [str(spec_a.id)]

    def test_tenant_and_service_filters_compose(self, spec_a, spec_b, service_a):
        rows = _rows(
            _api().get(
                f"{SPECIALISTS_URL}?tenant={spec_a.tenant_id}"
                f"&service_id={service_a.id}",
            )
        )
        assert [row["id"] for row in rows] == [str(spec_a.id)]
