"""AMD-019 — create boundary for SalonService-catalog bookings.

Resolution + validation pass identically to the slots path, but the
booking is STOPPED at the persistence boundary (Appointment.service FK
barrier, owner stop-condition): a dedicated domain error → 409
SALON_SERVICE_BOOKING_UNSUPPORTED, and NO Appointment is written.
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
from appointments.domain.exceptions import SalonServiceBookingNotPersistableError
from appointments.models import Appointment
from services.models import (
    SalonService,
    ServiceCategory,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

VALID_TOKEN = "test-ayla-internal-token-amd019c"
EXTERNAL_USER_ID = "bot:amd019c"
CREATE_URL = "/api/v1/internal/appointments/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79996199001", is_proxy=True,
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="amd019c-t", name="AMD019C Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="amd019c_spec", password="x", role="specialist",
        phone="+79996199002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "AMD019C Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="AMD019C Cat", slug="amd019c-cat")


@pytest.fixture
def salon(tenant, category):
    return SalonService.objects.create(
        tenant=tenant, category=category, name="AMD019C Salon",
        duration_minutes=60,
    )


@pytest.fixture
def salon_link(salon, specialist):
    return SpecialistService.objects.create(
        salon_service=salon, specialist=specialist,
        duration_minutes=None, price=Decimal("2000.00"), is_active=True,
    )


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _body(customer, specialist, service_id) -> dict:
    return {
        "client_id": str(customer.id),
        "specialist_id": str(specialist.id),
        "service_id": str(service_id),
        "start_datetime": _future_iso(3),
    }


@pytest.mark.django_db
class TestCreateSalonBoundary:
    def test_create_salon_booking_stops_at_persistence_boundary(
        self, customer, specialist, salon, salon_link,
    ):
        """Valid salon service → resolution + validation pass → 409
        SALON_SERVICE_BOOKING_UNSUPPORTED at the boundary; NOTHING is
        written (no Appointment, no TUR grant)."""
        r = _api().post(
            CREATE_URL, _body(customer, specialist, salon.id), format="json",
        )
        assert r.status_code == 409, r.data
        assert r.data["error"]["code"] == "SALON_SERVICE_BOOKING_UNSUPPORTED"
        # NOT ServiceNotActiveError — the service is valid (AMD-019).
        assert r.data["error"]["code"] != "SERVICE_NOT_ACTIVE"
        assert Appointment.objects.count() == 0
        assert not TenantUserRelationship.objects.filter(
            user=customer, tenant=specialist.tenant,
        ).exists()

    def test_domain_error_type_at_service_level(
        self, customer, specialist, salon_link,
    ):
        """The boundary raises the DEDICATED domain error (stable slug
        carrier), not a generic booking error."""
        dto = CreateBookingDTO(
            client_id=customer.id,
            specialist_id=specialist.id,
            service_id=salon_link.salon_service_id,
            start_at=(
                datetime.now(tz=timezone.utc) + timedelta(hours=3)
            ),
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(SalonServiceBookingNotPersistableError):
            CreateBookingService().execute(dto)
        assert Appointment.objects.count() == 0

    def test_salon_without_link_is_unavailable_for_specialist(
        self, customer, specialist, salon,
    ):
        """No active link → the create surface maps the resolver error
        to the existing 422 SERVICE_NOT_ACTIVE (no existence leak)."""
        r = _api().post(
            CREATE_URL, _body(customer, specialist, salon.id), format="json",
        )
        assert r.status_code == 422
        assert r.data["error"]["code"] == "SERVICE_NOT_ACTIVE"
        assert Appointment.objects.count() == 0
