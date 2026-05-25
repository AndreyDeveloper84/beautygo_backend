"""Tests for GET /api/v1/users/me/tenant-relationships/ (#246 sub-phase 1.F)."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import TenantUserRelationship, User


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="tr-a", name="TR Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="tr-b", name="TR Tenant B")


@pytest.fixture
def anna(db):
    return User.objects.create_user(
        username="tr_anna", password="x", role="client",
        phone="+79991199001",
    )


def _client(user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestMyTenantRelationshipsEndpoint:
    """Pin the canonical multi-provider list endpoint."""

    def test_unauthenticated_returns_401(self):
        """AppTypeMiddleware runs before auth — must send X-App-Type to
        reach the IsAuthenticated check (otherwise 403 from middleware,
        not the 401 we want to pin)."""
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        r = c.get("/api/v1/users/me/tenant-relationships/")
        assert r.status_code == 401

    def test_empty_list_for_user_with_no_tur(self, anna):
        TenantUserRelationship.objects.filter(user=anna).delete()
        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["data"] == []

    def test_returns_all_active_tur_ordered_by_granted_at_desc(
        self, anna, tenant_a, tenant_b,
    ):
        TenantUserRelationship.objects.filter(user=anna).delete()
        old = TenantUserRelationship.objects.create(
            user=anna, tenant=tenant_a,
        )
        new = TenantUserRelationship.objects.create(
            user=anna, tenant=tenant_b,
        )

        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        assert r.status_code == 200
        items = r.json()["data"]["data"]
        assert len(items) == 2
        # Order: most-recently-granted first.
        assert items[0]["tenant_id"] == str(new.tenant_id)
        assert items[1]["tenant_id"] == str(old.tenant_id)
        # Slug + name surfaced from joined tenant row.
        assert items[0]["tenant_slug"] == "tr-b"
        assert items[0]["tenant_name"] == "TR Tenant B"
        # Default role per founder ack: customer.
        assert items[0]["role"] == "customer"

    def test_revoked_tur_excluded(self, anna, tenant_a, tenant_b):
        from django.utils import timezone
        TenantUserRelationship.objects.filter(user=anna).delete()
        TenantUserRelationship.objects.create(user=anna, tenant=tenant_a)
        revoked = TenantUserRelationship.objects.create(
            user=anna, tenant=tenant_b,
        )
        revoked.is_active = False
        revoked.revoked_at = timezone.now()
        revoked.revoke_reason = "test"
        revoked.save()

        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        assert r.status_code == 200
        items = r.json()["data"]["data"]
        # Only the active tenant_a row returned.
        assert len(items) == 1
        assert items[0]["tenant_id"] == str(tenant_a.id)

    def test_returns_only_callers_own_relationships(
        self, anna, tenant_a, tenant_b,
    ):
        """Another user's TURs MUST NOT leak."""
        other = User.objects.create_user(
            username="tr_other", password="x", role="client",
            phone="+79991199002",
        )
        TenantUserRelationship.objects.filter(user=anna).delete()
        TenantUserRelationship.objects.filter(user=other).delete()
        TenantUserRelationship.objects.create(user=anna, tenant=tenant_a)
        TenantUserRelationship.objects.create(user=other, tenant=tenant_b)

        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        items = r.json()["data"]["data"]
        assert len(items) == 1
        assert items[0]["tenant_id"] == str(tenant_a.id)

    def test_staff_role_visible(self, anna, tenant_a):
        TenantUserRelationship.objects.filter(user=anna).delete()
        TenantUserRelationship.objects.create(
            user=anna, tenant=tenant_a,
            role=TenantUserRelationship.Role.STAFF,
        )
        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        items = r.json()["data"]["data"]
        assert items[0]["role"] == "staff"
