"""Regression tests for #246 sub-phase 1.C — multi-provider customer access.

Pins the headline invariant from PR #149 design doc:
- A customer with N active TenantUserRelationship rows can:
  - GET /api/v1/appointments/ WITHOUT X-Tenant → sees bookings
    across ALL her tenants (global customer-wide list).
  - GET /api/v1/appointments/?X-Tenant=tenant_a → sees only
    tenant_a bookings (scoped view).
  - GET /api/v1/appointments/?X-Tenant=tenant_b → sees only
    tenant_b bookings.
  - GET /api/v1/appointments/?X-Tenant=stranger → 403 (no TUR).

Sub-phase 1.B added the TUR-based IsTenantMember + User.post_save
bridge. Sub-phase 1.C confirms the AppointmentViewSet permission
stack honours the multi-provider semantics end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="mp-tenant-a", name="MP Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="mp-tenant-b", name="MP Tenant B")


@pytest.fixture
def tenant_c_stranger(db):
    """Customer is NOT a member of this tenant."""
    return Tenant.objects.create(slug="mp-tenant-c", name="MP Tenant C")


@pytest.fixture
def anna(db, tenant_a, tenant_b):
    """Multi-provider customer — TUR(anna, A) + TUR(anna, B)."""
    u = User.objects.create_user(
        username="mp_anna", password="x", role="client",
        phone="+79991C00000",
    )
    # Direct TUR creation — the User.post_save bridge only handles
    # ONE tenant per save() (the current user.tenant_id). A
    # multi-provider customer needs N TURs created explicitly. In the
    # real flow Variant E (sub-phase 1.D) creates each TUR inside the
    # booking transaction.
    TenantUserRelationship.objects.create(user=u, tenant=tenant_a)
    TenantUserRelationship.objects.create(user=u, tenant=tenant_b)
    return u


@pytest.fixture
def specialist_a(db, tenant_a):
    u = User.objects.create_user(
        username="mp_spec_a", password="x", role="specialist",
        phone="+79991C00001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant_a
    p.display_name = "Spec A"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def specialist_b(db, tenant_b):
    u = User.objects.create_user(
        username="mp_spec_b", password="x", role="specialist",
        phone="+79991C00002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant_b
    p.display_name = "Spec B"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="MP Cat", slug="mp-cat")


@pytest.fixture
def service_a(specialist_a, category):
    return Service.objects.create(
        specialist=specialist_a, category=category,
        name="MP Service A", price=Decimal("1500.00"),
        duration_minutes=60, is_active=True, buffer_after_minutes=0,
    )


@pytest.fixture
def service_b(specialist_b, category):
    return Service.objects.create(
        specialist=specialist_b, category=category,
        name="MP Service B", price=Decimal("1500.00"),
        duration_minutes=60, is_active=True, buffer_after_minutes=0,
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _booking(client_user, specialist, service, hours_ahead: int = 3):
    """Create a confirmed booking via the real service."""
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(hours_ahead),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    return appt


def _client(user, *, tenant_slug: str | None = None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    if tenant_slug is not None:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestMultiProviderCustomerAccess:
    """#246 design doc headline: customer with multiple active TURs
    accesses bookings in each tenant context."""

    def test_global_list_returns_all_tenants(
        self, anna, specialist_a, service_a, specialist_b, service_b,
        tenant_a, tenant_b,
    ):
        """Anna without X-Tenant → sees bookings from both A and B."""
        appt_a = _booking(anna, specialist_a, service_a, hours_ahead=3)
        appt_b = _booking(anna, specialist_b, service_b, hours_ahead=5)

        r = _client(anna).get("/api/v1/appointments/")
        assert r.status_code == 200, r.data
        body = r.json()
        data = body.get("data") if isinstance(body, dict) else body
        items = data if isinstance(data, list) else data.get("results", [])
        ids = {i["id"] for i in items}
        assert str(appt_a.id) in ids
        assert str(appt_b.id) in ids

    def test_scoped_list_returns_only_that_tenant(
        self, anna, specialist_a, service_a, specialist_b, service_b,
        tenant_a, tenant_b,
    ):
        """Anna with X-Tenant=A → sees ONLY tenant_a bookings."""
        appt_a = _booking(anna, specialist_a, service_a, hours_ahead=3)
        appt_b = _booking(anna, specialist_b, service_b, hours_ahead=5)

        r = _client(anna, tenant_slug=tenant_a.slug).get(
            "/api/v1/appointments/",
        )
        assert r.status_code == 200, r.data
        body = r.json()
        data = body.get("data") if isinstance(body, dict) else body
        items = data if isinstance(data, list) else data.get("results", [])
        ids = {i["id"] for i in items}
        assert str(appt_a.id) in ids
        assert str(appt_b.id) not in ids

    def test_xtenant_no_tur_returns_403(
        self, anna, tenant_c_stranger,
    ):
        """Anna with X-Tenant=C (no TUR) → 403 via IsTenantMember."""
        r = _client(anna, tenant_slug=tenant_c_stranger.slug).get(
            "/api/v1/appointments/",
        )
        assert r.status_code == 403

    def test_retrieve_other_tenant_appt_via_global_route(
        self, anna, specialist_a, service_a, specialist_b, service_b,
    ):
        """Without X-Tenant: detail endpoint serves appts from any
        of Anna's tenants — they're all 'hers' from her POV."""
        appt_b = _booking(anna, specialist_b, service_b, hours_ahead=5)
        r = _client(anna).get(f"/api/v1/appointments/{appt_b.id}/")
        assert r.status_code == 200, r.data

    def test_retrieve_via_wrong_tenant_scope_returns_404(
        self, anna, specialist_a, service_a, specialist_b, service_b,
        tenant_a,
    ):
        """Anna with X-Tenant=A trying to retrieve a tenant_b booking
        → 404 via queryset filter (`qs.filter(tenant=request.tenant)`).
        IsTenantMember passes (Anna has TUR(A)), but the row is
        filtered out by tenant_id mismatch."""
        appt_b = _booking(anna, specialist_b, service_b, hours_ahead=5)
        r = _client(anna, tenant_slug=tenant_a.slug).get(
            f"/api/v1/appointments/{appt_b.id}/",
        )
        assert r.status_code == 404
