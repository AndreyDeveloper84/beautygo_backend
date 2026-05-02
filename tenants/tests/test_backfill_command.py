"""Tests for `python manage.py backfill_tenants` (DRF-242.4)."""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture
def out():
    return StringIO()


class TestBackfillTenantsCommand:
    def test_creates_default_tenant_if_missing(self, out):
        assert not Tenant.all_objects.filter(slug="formula").exists()
        call_command("backfill_tenants", "--slug", "formula", "--name", "F", stdout=out)
        assert Tenant.all_objects.filter(slug="formula").exists()
        assert "Created tenant 'formula'" in out.getvalue()

    def test_idempotent_reuses_existing_tenant(self, out):
        Tenant.objects.create(slug="formula", name="F")
        call_command("backfill_tenants", "--slug", "formula", stdout=out)
        assert Tenant.all_objects.filter(slug="formula").count() == 1
        assert "Using existing tenant" in out.getvalue()

    def test_dry_run_writes_nothing(self, out):
        from users.models import User
        u = User.objects.create_user(
            username="legacy", phone="+79991110050", role="client",
        )
        assert u.tenant_id is None

        call_command(
            "backfill_tenants", "--slug", "ghost", "--dry-run", stdout=out,
        )

        u.refresh_from_db()
        assert u.tenant_id is None
        assert not Tenant.all_objects.filter(slug="ghost").exists()
        assert "[dry-run] would create" in out.getvalue()

    def test_backfills_user_and_specialist_profile(self, out):
        from users.models import SpecialistProfile, User

        client = User.objects.create_user(
            username="legacyclient", phone="+79991110051", role="client",
        )
        spec_user = User.objects.create_user(
            username="legacyspec", phone="+79991110052", role="specialist",
        )
        # Signal auto-created the SpecialistProfile.
        sp = spec_user.specialist_profile
        assert client.tenant_id is None
        assert sp.tenant_id is None

        call_command("backfill_tenants", "--slug", "formula", "--name", "F", stdout=out)

        client.refresh_from_db()
        sp.refresh_from_db()
        tenant = Tenant.objects.get(slug="formula")
        assert client.tenant_id == tenant.id
        assert sp.tenant_id == tenant.id

    def test_backfills_food_scan_inheriting_user_tenant(self, out):
        from nutrition.models import FoodScan
        from users.models import User

        u = User.objects.create_user(
            username="lf", phone="+79991110053", role="client",
        )
        scan = FoodScan.objects.create(user=u)
        assert scan.tenant_id is None

        call_command("backfill_tenants", "--slug", "formula", "--name", "F", stdout=out)

        scan.refresh_from_db()
        u.refresh_from_db()
        assert scan.tenant_id == u.tenant_id
        assert scan.tenant_id is not None
