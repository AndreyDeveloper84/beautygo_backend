"""Phase 0 (Variant B) — cross-tenant POST /appointments/ returns 404.

Pins the interim integrity fix:
- Anna (user.tenant=A, X-Tenant: A) tries to create a booking with
  Olga (specialist.tenant=B). Post-#142 IsTenantMember accepted
  because the header matched the user; CreateBookingService stamped
  tenant_id=B on the row; the strict queryset filter made the row
  unreachable. Net: silent integrity bug, booking stranded.
- Phase 0 fix in AppointmentCreateSerializer.validate raises 404
  "Specialist not found." before the service runs. No row created.

Phase 1 will replace this with Variant E (invisible TUR grant via
#246). When Phase 1 lands, this test changes shape — the create
should succeed silently and the response should include a normal
appointment payload.
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
from users.models import SpecialistProfile, User


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="ct404-a", name="Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="ct404-b", name="Tenant B")


@pytest.fixture
def anna_in_a(db, tenant_a):
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
    p.display_name = "Ольга"
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
        name="Маникюр у Ольги",
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
class TestCrossTenantCreate404:
    def test_anna_a_books_olga_b_returns_404(
        self, anna_in_a, olga_in_b, service_in_b, tenant_a,
    ):
        """The headline case: client in tenant A books specialist in
        tenant B with X-Tenant=A. Serializer raises 404 before the
        booking service runs."""
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
        # No row should have been created.
        assert Appointment.objects.count() == 0

    def test_same_tenant_create_still_works(
        self, anna_in_a, olga_in_b, service_in_b, tenant_b,
    ):
        """Sanity: same-tenant booking succeeds. Anna sends
        X-Tenant=B to confirm the 404 path is cross-tenant-specific,
        not "any tenant header at all blocks." (She wouldn't normally
        do this, but the serializer behaviour should match the
        request_tenant input.)"""
        # Anna goes into tenant B context for the request.
        # Pre-#246 this wouldn't pass IsTenantMember; we test the
        # serializer alone via DRF's APIRequestFactory.
        from rest_framework.test import APIRequestFactory
        from appointments.serializers import AppointmentCreateSerializer

        factory = APIRequestFactory()
        request = factory.post("/api/v1/appointments/")
        request.user = anna_in_a
        request.tenant = tenant_b  # mimic middleware setting

        ser = AppointmentCreateSerializer(
            data={
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            context={"request": request},
        )
        assert ser.is_valid(), ser.errors  # validator passes

    def test_no_tenant_header_skips_check(
        self, anna_in_a, olga_in_b, service_in_b,
    ):
        """Permissive Phase 0 rollout: when X-Tenant is absent,
        request.tenant is None and the cross-tenant validator is a
        no-op. Other layers (IsTenantMember + queryset) still apply."""
        from rest_framework.test import APIRequestFactory
        from appointments.serializers import AppointmentCreateSerializer

        factory = APIRequestFactory()
        request = factory.post("/api/v1/appointments/")
        request.user = anna_in_a
        request.tenant = None

        ser = AppointmentCreateSerializer(
            data={
                "specialist_id": str(olga_in_b.id),
                "service_id": str(service_in_b.id),
                "start_datetime": _future_iso(3),
            },
            context={"request": request},
        )
        assert ser.is_valid(), ser.errors

    def test_missing_specialist_falls_through(
        self, anna_in_a, tenant_a,
    ):
        """If specialist_id points to nothing, the cross-tenant guard
        is silent and downstream validation handles the missing-FK
        case."""
        from rest_framework.test import APIRequestFactory
        from appointments.serializers import AppointmentCreateSerializer

        factory = APIRequestFactory()
        request = factory.post("/api/v1/appointments/")
        request.user = anna_in_a
        request.tenant = tenant_a

        ser = AppointmentCreateSerializer(
            data={
                "specialist_id": str(uuid4()),  # nonexistent
                "service_id": str(uuid4()),
                "start_datetime": _future_iso(3),
            },
            context={"request": request},
        )
        # Validator returns silently — downstream (service) raises
        # the proper not-found / not-available error.
        assert ser.is_valid(), ser.errors
