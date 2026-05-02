"""Cross-app tests for the new tenant FKs (DRF-242.3).

Lives in tenants/tests/ rather than per-app to keep all multi-tenant
invariants discoverable in one place. Each test is small and asserts
a single property: the FK exists, is nullable, and is PROTECTed against
accidental tenant deletion.

Backfill / scoping behaviour are out of scope here — they belong to
DRF-242.4 once the feature flag and middleware land.
"""
from __future__ import annotations

import pytest
from django.db.models.deletion import ProtectedError

from tenants.models import Tenant


pytestmark = pytest.mark.django_db


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="protocheck", name="Proto")


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class TestUserTenantFK:
    def test_default_user_has_no_tenant(self):
        from users.models import User
        u = User.objects.create_user(
            username="solo", phone="+79991110011", role="client",
        )
        assert u.tenant is None
        assert u.tenant_id is None

    def test_user_can_be_assigned_a_tenant(self, tenant):
        from users.models import User
        u = User.objects.create_user(
            username="bound", phone="+79991110012", role="client",
            tenant=tenant,
        )
        u.refresh_from_db()
        assert u.tenant_id == tenant.id

    def test_tenant_delete_protected_when_users_reference_it(self, tenant):
        from users.models import User
        User.objects.create_user(
            username="protector", phone="+79991110013", role="client",
            tenant=tenant,
        )
        with pytest.raises(ProtectedError):
            tenant.delete()


# ---------------------------------------------------------------------------
# SpecialistProfile
# ---------------------------------------------------------------------------


class TestSpecialistProfileTenantFK:
    """SpecialistProfile is auto-created by users.signals.post_save when a
    User with role='specialist' is saved. So tests fetch the existing
    profile and assign the tenant rather than instantiating a new one
    (which would trip the UNIQUE constraint on user_id)."""

    def test_specialist_can_be_assigned_a_tenant(self, tenant):
        from users.models import User
        u = User.objects.create_user(
            username="spec", phone="+79991110021", role="specialist",
        )
        sp = u.specialist_profile
        sp.tenant = tenant
        sp.save(update_fields=["tenant"])
        sp.refresh_from_db()
        assert sp.tenant_id == tenant.id

    def test_tenant_delete_protected_when_specialists_reference_it(self, tenant):
        from users.models import User
        u = User.objects.create_user(
            username="spec2", phone="+79991110022", role="specialist",
        )
        sp = u.specialist_profile
        sp.tenant = tenant
        sp.save(update_fields=["tenant"])
        with pytest.raises(ProtectedError):
            tenant.delete()


# ---------------------------------------------------------------------------
# Appointment
# ---------------------------------------------------------------------------


class TestAppointmentTenantFK:
    def test_appointment_can_be_assigned_a_tenant(self, tenant):
        from datetime import datetime, timedelta, timezone as dt_tz
        from appointments.models import Appointment
        from services.models import Service
        from users.models import User

        client = User.objects.create_user(
            username="ac1", phone="+79991110031", role="client",
        )
        spec_user = User.objects.create_user(
            username="as1", phone="+79991110032", role="specialist",
        )
        # Signal auto-created the profile — fetch it.
        spec = spec_user.specialist_profile
        svc = Service.objects.create(
            specialist=spec, name="Test", price=1000, duration_minutes=60,
        )
        start = datetime(2026, 6, 1, 10, 0, tzinfo=dt_tz.utc)
        appt = Appointment.objects.create(
            client=client, specialist=spec, service=svc,
            start_datetime=start, end_datetime=start + timedelta(hours=1),
            price=1000,
            tenant=tenant,
        )
        appt.refresh_from_db()
        assert appt.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# FoodScan
# ---------------------------------------------------------------------------


class TestFoodScanTenantFK:
    def test_foodscan_can_be_assigned_a_tenant(self, tenant):
        from nutrition.models import FoodScan
        from users.models import User
        u = User.objects.create_user(
            username="fc1", phone="+79991110041", role="client",
        )
        scan = FoodScan.objects.create(user=u, tenant=tenant)
        scan.refresh_from_db()
        assert scan.tenant_id == tenant.id

    def test_tenant_delete_protected_when_scans_reference_it(self, tenant):
        from nutrition.models import FoodScan
        from users.models import User
        u = User.objects.create_user(
            username="fc2", phone="+79991110042", role="client",
        )
        FoodScan.objects.create(user=u, tenant=tenant)
        with pytest.raises(ProtectedError):
            tenant.delete()
