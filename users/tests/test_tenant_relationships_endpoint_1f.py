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


@pytest.mark.django_db
class TestOrderingIsTotal:
    """DRF-1127 — ordering must be a total order, not just `-granted_at`.

    ``granted_at`` is ``auto_now_add``: a bulk grant (import, migration,
    a single request that grants several tenants) stamps every row from
    the same transaction clock, so ties are the normal case, not an
    exotic one. With only ``-granted_at`` in ``order_by`` the DB is free
    to return tied rows in any order, and two calls may disagree — the
    caller cannot page or diff the list reliably.

    ``test_returns_all_active_tur_ordered_by_granted_at_desc`` above does
    NOT cover this: its two rows get distinct ``granted_at`` values, so
    the primary key alone already decides the order and the missing
    tie-break never shows.
    """

    def test_tied_granted_at_rows_come_back_in_a_deterministic_order(
        self, anna,
    ):
        from datetime import timedelta
        from uuid import UUID

        from django.utils import timezone

        TenantUserRelationship.objects.filter(user=anna).delete()

        # Explicit, monotonically increasing ids so heap (insertion)
        # order is the exact opposite of the `-id` tie-break the
        # endpoint must apply. Without the tie-break the DB returns
        # insertion order and the assertion below fails deterministically
        # rather than by luck.
        rows = []
        for i in range(6):
            tenant = Tenant.objects.create(
                slug=f"tie-{i}", name=f"Tie Tenant {i}",
            )
            rows.append(
                TenantUserRelationship.objects.create(
                    id=UUID(int=i + 1),
                    user=anna,
                    tenant=tenant,
                )
            )

        # granted_at is auto_now_add — force the tie explicitly.
        # Offset from now(), never a literal date.
        stamp = timezone.now() - timedelta(hours=1)
        TenantUserRelationship.objects.filter(
            id__in=[r.id for r in rows],
        ).update(granted_at=stamp)

        # Positive guard on the same data: the rows really do share one
        # granted_at. Without this, the ordering assertion below would be
        # green on data where no tie exists and would prove nothing.
        stamps = set(
            TenantUserRelationship.objects
            .filter(id__in=[r.id for r in rows])
            .values_list("granted_at", flat=True)
        )
        assert len(stamps) == 1, (
            f"fixture failed to build tied granted_at values: {stamps}"
        )

        r = _client(anna).get("/api/v1/users/me/tenant-relationships/")
        assert r.status_code == 200
        items = r.json()["data"]["data"]
        assert len(items) == len(rows)

        expected = [
            str(row.tenant_id)
            for row in sorted(rows, key=lambda x: x.id, reverse=True)
        ]
        assert [it["tenant_id"] for it in items] == expected, (
            "tied granted_at rows came back in DB-arbitrary order — "
            "ordering has no secondary key"
        )

    def test_repeated_calls_agree_on_the_order_of_tied_rows(self, anna):
        """Same data, two calls: the list must not reshuffle."""
        from datetime import timedelta
        from uuid import UUID

        from django.utils import timezone

        TenantUserRelationship.objects.filter(user=anna).delete()

        rows = []
        for i in range(6):
            tenant = Tenant.objects.create(
                slug=f"tie2-{i}", name=f"Tie2 Tenant {i}",
            )
            rows.append(
                TenantUserRelationship.objects.create(
                    id=UUID(int=100 + i),
                    user=anna,
                    tenant=tenant,
                )
            )
        stamp = timezone.now() - timedelta(hours=2)
        TenantUserRelationship.objects.filter(
            id__in=[r.id for r in rows],
        ).update(granted_at=stamp)

        stamps = set(
            TenantUserRelationship.objects
            .filter(id__in=[r.id for r in rows])
            .values_list("granted_at", flat=True)
        )
        assert len(stamps) == 1, "positive guard: rows must share granted_at"

        c = _client(anna)
        first = [
            it["tenant_id"]
            for it in c.get(
                "/api/v1/users/me/tenant-relationships/",
            ).json()["data"]["data"]
        ]
        second = [
            it["tenant_id"]
            for it in c.get(
                "/api/v1/users/me/tenant-relationships/",
            ).json()["data"]["data"]
        ]
        assert first == second
        assert len(set(first)) == len(rows)
