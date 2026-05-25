"""Tests for TenantUserRelationship model — sub-phase 1.A of #246.

Pin:
- Schema β: at most one active row per (user, tenant); inactive rows
  pile up as history. Re-grant after revoke creates new row.
- Role choices: customer / staff / admin enforced via Django choices.
- Backfill migration (0012) creates exactly one active TUR per
  pre-existing User.tenant pair; idempotent on rerun.
- Granted_by tracks origin (self / admin / system).
"""
from __future__ import annotations

import importlib

import pytest
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

from tenants.models import Tenant
from users.models import TenantUserRelationship, User


# Migration callable imported via importlib (module starts with digit).
_mig = importlib.import_module(
    "users.migrations.0012_tenantuserrelationship",
)
backfill_tur_from_user_tenant = _mig.backfill_tur_from_user_tenant


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="t246a", name="Tenant A 246")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="t246b", name="Tenant B 246")


@pytest.fixture
def alice(db):
    return User.objects.create_user(
        username="t246_alice", password="x", role="client",
        phone="+79991246000",
    )


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="t246_spec", password="x", role="specialist",
        phone="+79991246001",
    )


@pytest.mark.django_db
class TestPartialUniqueConstraint:
    """Schema β invariant — at most one active row per (user, tenant)."""

    def test_active_uniqueness_enforced(self, alice, tenant_a):
        TenantUserRelationship.objects.create(user=alice, tenant=tenant_a)
        with pytest.raises(IntegrityError):
            TenantUserRelationship.objects.create(
                user=alice, tenant=tenant_a,
            )

    def test_revoked_row_does_not_block_new_active(self, alice, tenant_a):
        """Re-grant after revoke creates a new row; old row stays as
        history with its revoked_at intact."""
        from django.utils import timezone

        first = TenantUserRelationship.objects.create(
            user=alice, tenant=tenant_a,
        )
        first.is_active = False
        first.revoked_at = timezone.now()
        first.revoke_reason = "test"
        first.save()

        second = TenantUserRelationship.objects.create(
            user=alice, tenant=tenant_a,
        )

        assert TenantUserRelationship.objects.filter(
            user=alice, tenant=tenant_a,
        ).count() == 2
        assert second.is_active is True
        # History preserved.
        first.refresh_from_db()
        assert first.is_active is False
        assert first.revoked_at is not None

    def test_multiple_revoked_rows_allowed(self, alice, tenant_a):
        """User can have N revoked rows for the same tenant —
        only the active row is unique. Models real history of
        grant→revoke cycles."""
        from django.utils import timezone

        for _ in range(3):
            tur = TenantUserRelationship.objects.create(
                user=alice, tenant=tenant_a,
            )
            tur.is_active = False
            tur.revoked_at = timezone.now()
            tur.save()

        # 3 revoked + 0 active = no constraint violation.
        assert TenantUserRelationship.objects.filter(
            user=alice, tenant=tenant_a, is_active=False,
        ).count() == 3


@pytest.mark.django_db
class TestRoleField:
    """Role choices: customer / staff / admin."""

    def test_default_role_is_customer(self, alice, tenant_a):
        tur = TenantUserRelationship.objects.create(
            user=alice, tenant=tenant_a,
        )
        assert tur.role == TenantUserRelationship.Role.CUSTOMER

    def test_staff_role_explicit(self, specialist_user, tenant_a):
        tur = TenantUserRelationship.objects.create(
            user=specialist_user, tenant=tenant_a, role="staff",
        )
        assert tur.role == TenantUserRelationship.Role.STAFF


@pytest.mark.django_db
class TestGrantedByTracking:
    """granted_by audits the origin: self / admin / system."""

    def test_default_granted_by_self(self, alice, tenant_a):
        tur = TenantUserRelationship.objects.create(
            user=alice, tenant=tenant_a,
        )
        assert tur.granted_by == "self"

    def test_system_grant_via_backfill(self, alice, tenant_a):
        tur = TenantUserRelationship.objects.create(
            user=alice, tenant=tenant_a, granted_by="system",
        )
        assert tur.granted_by == "system"


@pytest.mark.django_db
class TestBackfillFromUserTenant:
    """Migration 0012 RunPython backfill correctness."""

    def test_backfill_creates_tur_per_user_with_tenant(
        self, alice, specialist_user, tenant_a,
    ):
        """User.tenant=A → exactly one active TUR(user, A)."""
        alice.tenant = tenant_a
        alice.save()
        specialist_user.tenant = tenant_a
        specialist_user.save()

        # Wipe any TURs that signals or fixtures created.
        TenantUserRelationship.objects.all().delete()

        backfill_tur_from_user_tenant(
            apps=connection.schema_editor().connection,
            schema_editor=connection.schema_editor(),
        )
        # `apps` parameter is used only for `get_model`; pass the
        # real Django apps registry for correctness.
        from django.apps import apps as real_apps
        backfill_tur_from_user_tenant(real_apps, connection.schema_editor())

        active = TenantUserRelationship.objects.filter(is_active=True)
        # Two users, one tenant each → 2 active rows.
        assert active.filter(user=alice).count() == 1
        assert active.filter(user=specialist_user).count() == 1

        # Role mapping correct: client → customer; specialist → staff.
        assert active.get(user=alice).role == "customer"
        assert active.get(user=specialist_user).role == "staff"

        # All backfill rows are system-granted.
        assert all(t.granted_by == "system" for t in active)

    def test_backfill_idempotent(self, alice, tenant_a):
        """Running backfill twice produces the same row count."""
        from django.apps import apps as real_apps

        alice.tenant = tenant_a
        alice.save()
        TenantUserRelationship.objects.all().delete()

        backfill_tur_from_user_tenant(real_apps, connection.schema_editor())
        first_count = TenantUserRelationship.objects.count()

        backfill_tur_from_user_tenant(real_apps, connection.schema_editor())
        second_count = TenantUserRelationship.objects.count()

        assert first_count == second_count == 1

    def test_backfill_skips_user_without_tenant(self, alice):
        """User.tenant=None must not generate a TUR row."""
        from django.apps import apps as real_apps

        alice.tenant = None
        alice.save()
        TenantUserRelationship.objects.all().delete()

        backfill_tur_from_user_tenant(real_apps, connection.schema_editor())

        assert TenantUserRelationship.objects.filter(user=alice).count() == 0


@pytest.mark.django_db
class TestMigrationApplied:
    """0012 graph + constraint sanity."""

    def test_migration_0012_in_graph(self):
        executor = MigrationExecutor(connection)
        names = {
            n[1] for n in executor.loader.graph.nodes if n[0] == "users"
        }
        assert "0012_tenantuserrelationship" in names

    def test_constraint_named_correctly(self):
        constraint_names = {
            c.name for c in TenantUserRelationship._meta.constraints
        }
        assert "tur_unique_active" in constraint_names
