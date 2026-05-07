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


# ---------------------------------------------------------------------------
# ServiceCategory (DRF-242.6)
# ---------------------------------------------------------------------------


class TestServiceCategoryTenantFK:
    def test_default_category_has_no_tenant(self):
        from services.models import ServiceCategory
        cat = ServiceCategory.objects.create(name="Маникюр solo")
        assert cat.tenant is None
        assert cat.tenant_id is None

    def test_category_can_be_assigned_a_tenant(self, tenant):
        from services.models import ServiceCategory
        cat = ServiceCategory.objects.create(name="Маникюр bound", tenant=tenant)
        cat.refresh_from_db()
        assert cat.tenant_id == tenant.id

    def test_tenant_delete_protected_when_categories_reference_it(self, tenant):
        from services.models import ServiceCategory
        ServiceCategory.objects.create(name="Маникюр protector", tenant=tenant)
        with pytest.raises(ProtectedError):
            tenant.delete()


# ---------------------------------------------------------------------------
# Service (DRF-242.6)
# ---------------------------------------------------------------------------


class TestServiceTenantFK:
    def test_service_can_be_assigned_a_tenant(self, tenant):
        from services.models import Service
        from users.models import User
        spec_user = User.objects.create_user(
            username="svc_spec1", phone="+79991110051", role="specialist",
        )
        spec = spec_user.specialist_profile
        svc = Service.objects.create(
            specialist=spec, name="Тест", price=1000, duration_minutes=60,
            tenant=tenant,
        )
        svc.refresh_from_db()
        assert svc.tenant_id == tenant.id

    def test_tenant_delete_protected_when_services_reference_it(self, tenant):
        from services.models import Service
        from users.models import User
        spec_user = User.objects.create_user(
            username="svc_spec2", phone="+79991110052", role="specialist",
        )
        spec = spec_user.specialist_profile
        Service.objects.create(
            specialist=spec, name="Защитник", price=1000, duration_minutes=60,
            tenant=tenant,
        )
        with pytest.raises(ProtectedError):
            tenant.delete()


# ---------------------------------------------------------------------------
# Review (DRF-242.6)
# ---------------------------------------------------------------------------


class TestReviewTenantFK:
    def test_review_can_be_assigned_a_tenant(self, tenant):
        from datetime import datetime, timedelta, timezone as dt_tz
        from appointments.models import Appointment
        from reviews.models import Review
        from services.models import Service
        from users.models import User

        client = User.objects.create_user(
            username="rv_client1", phone="+79991110061", role="client",
        )
        spec_user = User.objects.create_user(
            username="rv_spec1", phone="+79991110062", role="specialist",
        )
        spec = spec_user.specialist_profile
        svc = Service.objects.create(
            specialist=spec, name="Тест", price=1000, duration_minutes=60,
        )
        start = datetime(2026, 6, 1, 10, 0, tzinfo=dt_tz.utc)
        appt = Appointment.objects.create(
            client=client, specialist=spec, service=svc,
            start_datetime=start, end_datetime=start + timedelta(hours=1),
            price=1000,
        )
        review = Review.objects.create(
            appointment=appt, client=client, specialist=spec, service=svc,
            rating=5, tenant=tenant,
        )
        review.refresh_from_db()
        assert review.tenant_id == tenant.id

    def test_tenant_delete_protected_when_reviews_reference_it(self, tenant):
        from datetime import datetime, timedelta, timezone as dt_tz
        from appointments.models import Appointment
        from reviews.models import Review
        from services.models import Service
        from users.models import User

        client = User.objects.create_user(
            username="rv_client2", phone="+79991110071", role="client",
        )
        spec_user = User.objects.create_user(
            username="rv_spec2", phone="+79991110072", role="specialist",
        )
        spec = spec_user.specialist_profile
        svc = Service.objects.create(
            specialist=spec, name="Защитник", price=1000, duration_minutes=60,
        )
        start = datetime(2026, 6, 2, 10, 0, tzinfo=dt_tz.utc)
        appt = Appointment.objects.create(
            client=client, specialist=spec, service=svc,
            start_datetime=start, end_datetime=start + timedelta(hours=1),
            price=1000,
        )
        Review.objects.create(
            appointment=appt, client=client, specialist=spec, service=svc,
            rating=4, tenant=tenant,
        )
        with pytest.raises(ProtectedError):
            tenant.delete()


# ---------------------------------------------------------------------------
# Cross-tenant isolation (DRF-242.6)
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    """Filtering by tenant must not leak data between tenants. These are
    the smallest possible reads that prove the FK + filter discipline
    works for the new DRF-242.6 surfaces."""

    def test_services_filter_by_tenant_is_isolated(self):
        from services.models import Service
        from users.models import User
        t1 = Tenant.objects.create(slug="iso-svc-a", name="A")
        t2 = Tenant.objects.create(slug="iso-svc-b", name="B")
        u1 = User.objects.create_user(
            username="iso_svc_u1", phone="+79991110081", role="specialist",
        )
        u2 = User.objects.create_user(
            username="iso_svc_u2", phone="+79991110082", role="specialist",
        )
        Service.objects.create(
            specialist=u1.specialist_profile, name="A-only",
            price=100, duration_minutes=30, tenant=t1,
        )
        Service.objects.create(
            specialist=u2.specialist_profile, name="B-only",
            price=100, duration_minutes=30, tenant=t2,
        )
        a_visible = list(Service.objects.filter(tenant=t1).values_list("name", flat=True))
        b_visible = list(Service.objects.filter(tenant=t2).values_list("name", flat=True))
        assert a_visible == ["A-only"]
        assert b_visible == ["B-only"]

    def test_categories_filter_by_tenant_is_isolated(self):
        from services.models import ServiceCategory
        t1 = Tenant.objects.create(slug="iso-cat-a", name="A")
        t2 = Tenant.objects.create(slug="iso-cat-b", name="B")
        ServiceCategory.objects.create(name="A taxonomy", tenant=t1)
        ServiceCategory.objects.create(name="B taxonomy", tenant=t2)
        a_visible = list(
            ServiceCategory.objects.filter(tenant=t1).values_list("name", flat=True)
        )
        assert a_visible == ["A taxonomy"]
