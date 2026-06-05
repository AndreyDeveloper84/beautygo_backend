"""Cross-tenant POST /appointments/ — Variant E invisible TUR grant (#246 1.D).

Historical context:
- Phase 0 (PR #148) raised 404 on cross-tenant create as an interim
  integrity guard. The file name still references "404" for that era.
- Sub-phase 1.D (this version) replaces the 404 with Variant E:
  the booking succeeds AND silently grants a `TenantUserRelationship`
  for the specialist's tenant. "Booking IS the consent gesture" per
  the `ayla-first-strategic-pivot` framing.

Pins (post-1.D):
- Anna in tenant A books Olga in tenant B → 201, new TUR(Anna, B) row.
- Same-tenant create unaffected (no TUR grant needed; serializer no-op).
- No X-Tenant header → serializer no-op (permissive customer-wide context).
- Missing specialist → serializer no-op, downstream raises proper error.
- Revoked TUR for the target tenant → 404 (F2 defense from PR #152).
- Idempotent: repeat cross-tenant create on same specialist's tenant
  doesn't create a second TUR row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="ct404-a", name="Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="ct404-b", name="Tenant B")


@pytest.fixture
def anna_in_a(db, tenant_a):
    """Customer in tenant A. User.post_save bridge auto-creates TUR(anna, A)."""
    u = User.objects.create_user(
        username="ct404_anna", password="x", role="client",
        phone="+79991404000",
    )
    u.tenant = tenant_a
    u.save(update_fields=["tenant"])
    return u


@pytest.fixture
def olga_in_b(db, tenant_b):
    """Specialist Olga registered in tenant B."""
    u = User.objects.create_user(
        username="ct404_olga", password="x", role="specialist",
        phone="+79991404001",
    )
    u.tenant = tenant_b
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant_b
    p.display_name = "Olga"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Cross-tenant", slug="ct404-cat")


@pytest.fixture
def service_in_b(olga_in_b, category):
    return Service.objects.create(
        specialist=olga_in_b,
        category=category,
        name="Manicure",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


@pytest.mark.django_db
class TestCrossTenantCreateVariantE:
    """Variant E: cross-tenant booking succeeds + invisibly grants TUR."""

    def test_anna_a_books_olga_b_succeeds_and_grants_tur(
        self, anna_in_a, olga_in_b, service_in_b, tenant_a, tenant_b,
    ):
        """Headline Variant E case: client in tenant A books
        specialist in tenant B with X-Tenant=A. Booking succeeds;
        TUR(Anna, tenant_b) is invisibly created."""
        assert not TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).exists()

        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.defaults["HTTP_X_TENANT"] = tenant_a.slug
        c.force_authenticate(user=anna_in_a)
        r = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            format="json",
        )
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get()
        assert appt.tenant_id == tenant_b.id
        assert TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).count() == 1

    def test_repeat_cross_tenant_create_does_not_duplicate_tur(
        self, anna_in_a, olga_in_b, service_in_b, tenant_a, tenant_b,
    ):
        """Second cross-tenant booking for same tenant_b doesn't
        create a second TUR row — get_or_create + partial unique."""
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.defaults["HTTP_X_TENANT"] = tenant_a.slug
        c.force_authenticate(user=anna_in_a)

        r1 = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            format="json",
        )
        assert r1.status_code == 201
        r2 = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(7),
            },
            format="json",
        )
        assert r2.status_code == 201
        assert TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).count() == 1

    def test_revoked_tur_blocks_cross_tenant_create(
        self, anna_in_a, olga_in_b, service_in_b, tenant_a, tenant_b,
    ):
        """F2 defense (PR #152 adversarial): if Anna's TUR for tenant_b
        was previously revoked by admin, Variant E MUST NOT silently
        re-grant. Returns 404 same as missing-specialist (info hiding).
        """
        from django.utils import timezone as dj_tz
        TenantUserRelationship.objects.create(
            user=anna_in_a, tenant=tenant_b,
            is_active=False,
            revoked_at=dj_tz.now(),
            revoke_reason="admin_pre_test_revoke",
        )

        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.defaults["HTTP_X_TENANT"] = tenant_a.slug
        c.force_authenticate(user=anna_in_a)
        r = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            format="json",
        )
        assert r.status_code == 404
        assert Appointment.objects.count() == 0
        assert not TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).exists()


@pytest.mark.django_db
class TestNonCrossTenantPaths:
    """Permissive branches: same-tenant, no-header, missing-FK."""

    def test_same_tenant_create_no_tur_change(
        self, anna_in_a, tenant_a,
    ):
        """Same-tenant booking: validator no-op, no new TUR row."""
        spec_user = User.objects.create_user(
            username="ct404_spec_a", password="x", role="specialist",
            phone="+79991404099",
        )
        spec_user.tenant = tenant_a
        spec_user.save(update_fields=["tenant"])
        p = SpecialistProfile.objects.get(user=spec_user)
        p.tenant = tenant_a
        p.display_name = "Spec A"
        p.status = SpecialistProfile.ProfileStatus.ACTIVE
        p.is_available = True
        p.is_booking_enabled = True
        p.timezone = "Europe/Moscow"
        p.save()
        category = ServiceCategory.objects.create(
            name="Same", slug="ct404-same",
        )
        service = Service.objects.create(
            specialist=p, category=category, name="Same Service",
            price=Decimal("1500"), duration_minutes=60, is_active=True,
        )

        tur_count_before = TenantUserRelationship.objects.filter(
            user=anna_in_a,
        ).count()

        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.defaults["HTTP_X_TENANT"] = tenant_a.slug
        c.force_authenticate(user=anna_in_a)
        r = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(p.id),
                "service_id": str(service.id),
                "start_datetime": _future_iso(3),
            },
            format="json",
        )
        assert r.status_code == 201
        assert TenantUserRelationship.objects.filter(
            user=anna_in_a,
        ).count() == tur_count_before

    def test_no_tenant_header_still_grants_tur(
        self, anna_in_a, olga_in_b, service_in_b, tenant_b,
    ):
        """No X-Tenant header → grant-on-first-booking STILL fires.

        Post-#1014 the grant is a single platform-wide rule keyed off
        the specialist's tenant, not request_tenant_id. The nationwide
        bot (and a mobile customer without X-Tenant) carry no client
        tenant scope, yet booking IS the consent gesture — so the
        booking proceeds AND TUR(Anna, tenant_b) is granted. This
        replaces the pre-#1014 behaviour where a header-less booking
        skipped the grant.
        """
        assert not TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).exists()

        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=anna_in_a)
        r = c.post(
            "/api/v1/appointments/",
            {
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            format="json",
        )
        assert r.status_code == 201
        appt = Appointment.objects.get()
        assert appt.tenant_id == tenant_b.id
        assert TenantUserRelationship.objects.filter(
            user=anna_in_a, tenant=tenant_b, is_active=True,
        ).count() == 1

    def test_missing_specialist_validator_silent(
        self, anna_in_a, tenant_a,
    ):
        """Missing specialist_id → validator skips silently; downstream
        raises proper error."""
        from rest_framework.test import APIRequestFactory
        from appointments.serializers import AppointmentCreateSerializer

        factory = APIRequestFactory()
        request = factory.post("/api/v1/appointments/")
        request.user = anna_in_a
        request.tenant = tenant_a

        ser = AppointmentCreateSerializer(
            data={
                "specialist_id": str(uuid4()),
                "service_id": str(uuid4()),
                "start_datetime": _future_iso(3),
            },
            context={"request": request},
        )
        assert ser.is_valid(), ser.errors
