"""Tests for POST /api/v1/masters/internal/by-yclients-staff-ids/ (#92).

W1 admin mini-app batch lookup. Non-§H.3 — standard auth tests plus
the cross-tenant boundary check required by the catalog-shape contract.
"""
from __future__ import annotations

import time

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-deadbeef"
URL = "/api/v1/masters/internal/by-yclients-staff-ids/"


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="m92-a", name="W1 Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="m92-b", name="W1 Tenant B")


def _make_specialist(
    tenant, yclients_staff_id: str, name: str, phone_suffix: str,
) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"m92-{phone_suffix}", password="x", role="specialist",
        phone=f"+79992{phone_suffix}",
    )
    # OneToOne with SpecialistProfile is auto-created by signal in some
    # codebases — but here the existing pattern is explicit get/update.
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.tenant = tenant
    profile.yclients_staff_id = yclients_staff_id
    profile.booking_source = SpecialistProfile.BookingSource.YCLIENTS
    profile.yclients_company_id = "yk-company-1"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    # No X-App-Type — /masters/internal/ is in middleware excludes.
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


@pytest.mark.django_db
class TestMastersBatchLookupAuth:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_authorization_denies(self, tenant_a):
        r = _api(bearer=None).post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["1"]},
            format="json",
        )
        assert r.status_code == 403

    def test_wrong_bearer_denies(self, tenant_a):
        r = _api(bearer="wrong-token").post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["1"]},
            format="json",
        )
        assert r.status_code == 403

    def test_non_bearer_scheme_denies(self, tenant_a):
        c = APIClient()
        c.defaults["HTTP_AUTHORIZATION"] = f"Basic {VALID_TOKEN}"
        r = c.post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["1"]},
            format="json",
        )
        assert r.status_code == 403


@pytest.mark.django_db
class TestMastersBatchLookupFailClosed:
    """Empty AYLA_INTERNAL_API_TOKEN → fail closed regardless of bearer."""

    @pytest.fixture(autouse=True)
    def _empty(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = ""

    def test_empty_token_denies(self, tenant_a):
        r = _api(bearer="anything").post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["1"]},
            format="json",
        )
        assert r.status_code == 403


@pytest.mark.django_db
class TestMastersBatchLookupResolution:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_single_id_resolves(self, tenant_a):
        anna = _make_specialist(tenant_a, "10001", "Анна", "0001")
        r = _api().post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["10001"]},
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert len(body["masters"]) == 1
        assert body["masters"][0]["id"] == str(anna.id)
        assert body["masters"][0]["yclients_staff_id"] == "10001"
        assert body["masters"][0]["display_name"] == "Анна"
        assert body["masters"][0]["tenant_id"] == str(tenant_a.id)
        assert body["not_found_staff_ids"] == []

    def test_batch_of_ten_resolves(self, tenant_a):
        for i in range(10):
            _make_specialist(
                tenant_a, f"2{i:04d}", f"Master {i}", f"1{i:03d}",
            )
        ids = [f"2{i:04d}" for i in range(10)]
        r = _api().post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ids},
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert len(body["masters"]) == 10
        assert body["not_found_staff_ids"] == []

    def test_all_not_found(self, tenant_a):
        r = _api().post(
            URL,
            {
                "tenant_id": str(tenant_a.id),
                "yclients_staff_ids": ["ghost-1", "ghost-2", "ghost-3"],
            },
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert body["masters"] == []
        assert body["not_found_staff_ids"] == ["ghost-1", "ghost-2", "ghost-3"]

    def test_mixed_found_and_not_found(self, tenant_a):
        _make_specialist(tenant_a, "3001", "Found-1", "2001")
        _make_specialist(tenant_a, "3002", "Found-2", "2002")
        r = _api().post(
            URL,
            {
                "tenant_id": str(tenant_a.id),
                "yclients_staff_ids": ["3001", "ghost", "3002", "missing"],
            },
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        found = {m["yclients_staff_id"] for m in body["masters"]}
        assert found == {"3001", "3002"}
        # not_found preserves request order minus matched.
        assert body["not_found_staff_ids"] == ["ghost", "missing"]


@pytest.mark.django_db
class TestMastersBatchLookupTenantBoundary:
    """Defense-in-depth — a leaked bearer must not enumerate across
    tenants without naming each one explicitly."""

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_cross_tenant_id_not_returned(self, tenant_a, tenant_b):
        # Same yclients_staff_id in both tenants — must only see tenant A's.
        in_a = _make_specialist(tenant_a, "4001", "In A", "3001")
        _make_specialist(tenant_b, "4001", "In B", "3002")
        r = _api().post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ["4001"]},
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert len(body["masters"]) == 1
        assert body["masters"][0]["id"] == str(in_a.id)
        assert body["masters"][0]["tenant_id"] == str(tenant_a.id)


@pytest.mark.django_db
class TestMastersBatchLookupValidation:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_tenant_id_400(self, tenant_a):
        r = _api().post(
            URL, {"yclients_staff_ids": ["1"]}, format="json",
        )
        assert r.status_code == 400

    def test_missing_ids_400(self, tenant_a):
        r = _api().post(
            URL, {"tenant_id": str(tenant_a.id)}, format="json",
        )
        assert r.status_code == 400

    def test_empty_ids_list_400(self, tenant_a):
        # min_length=1 on the ListField — empty list violates contract.
        r = _api().post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": []},
            format="json",
        )
        assert r.status_code == 400

    def test_batch_size_cap(self, tenant_a):
        # 201 ids exceeds MAX_BATCH_SIZE=200 → 400.
        ids = [str(i) for i in range(201)]
        r = _api().post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ids},
            format="json",
        )
        assert r.status_code == 400

    def test_duplicate_ids_deduplicated(self, tenant_a):
        _make_specialist(tenant_a, "5001", "Dup", "4001")
        r = _api().post(
            URL,
            {
                "tenant_id": str(tenant_a.id),
                "yclients_staff_ids": ["5001", "5001", "5001", "ghost"],
            },
            format="json",
        )
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert len(body["masters"]) == 1
        assert body["not_found_staff_ids"] == ["ghost"]


@pytest.mark.django_db
class TestMastersBatchLookupPerformance:
    """The W1 admin operator syncs salon-rosters in single requests.
    A 50-master batch must round-trip well under 200ms server-side
    (excluding network). This pins the IN-clause index path."""

    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_batch_of_50_under_200ms(self, tenant_a):
        for i in range(50):
            _make_specialist(
                tenant_a, f"6{i:04d}", f"Perf {i}", f"5{i:03d}",
            )
        ids = [f"6{i:04d}" for i in range(50)]
        c = _api()
        start = time.monotonic()
        r = c.post(
            URL,
            {"tenant_id": str(tenant_a.id), "yclients_staff_ids": ids},
            format="json",
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        assert r.status_code == 200
        body = r.json()["data"]["data"]
        assert len(body["masters"]) == 50
        # Local fast-DB budget: 200ms is generous, comfortable headroom
        # over the actual query cost which sits well under 50ms with
        # the db_index on yclients_staff_id.
        assert elapsed_ms < 200, (
            f"batch-of-50 took {elapsed_ms:.1f}ms; "
            "exceeded 200ms budget — index missing or N+1?"
        )
