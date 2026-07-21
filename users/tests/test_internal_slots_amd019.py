"""AMD-019 — internal slots surface with the shared resolver.

GET /api/v1/internal/specialists/{id}/slots/ accepts BOTH marketplace
Service.id and SalonService.id (with an active SpecialistService link).
The PUBLIC slots endpoint keeps the marketplace-only lookup —
observable behaviour unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import SpecialistWorkingHours
from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-amd019"
INTERNAL_URL = "/api/v1/internal/specialists/"
PUBLIC_URL = "/api/v1/specialists/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="amd019-t", name="AMD019 Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="amd019_spec", password="x", role="specialist",
        phone="+79996190001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "AMD019 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="AMD019 Cat", slug="amd019-cat")


@pytest.fixture
def marketplace_service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="AMD019 Mkt",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def salon_link(specialist, tenant, category):
    salon = SalonService.objects.create(
        tenant=tenant, category=category, name="AMD019 Salon",
        duration_minutes=60,
    )
    link = SpecialistService.objects.create(
        salon_service=salon, specialist=specialist,
        duration_minutes=None, price=Decimal("2000.00"), is_active=True,
    )
    return link


@pytest.fixture
def working_day(specialist):
    target = (datetime.now(tz=timezone.utc) + timedelta(days=7)).date()
    SpecialistWorkingHours.objects.create(
        specialist=specialist,
        day_of_week=target.weekday(),
        is_working_day=True,
        start_time="09:00",
        end_time="18:00",
    )
    return target


def _api(*, bearer=VALID_TOKEN):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _slots_url(base, specialist, service_id, target):
    return (
        f"{base}{specialist.id}/slots/"
        f"?service_id={service_id}&date={target.isoformat()}"
    )


@pytest.mark.django_db
class TestInternalSlotsAmd019:
    def test_marketplace_service_gives_slots(
        self, specialist, marketplace_service, working_day,
    ):
        """Regression: the marketplace path keeps working through the
        resolver."""
        r = _api().get(_slots_url(
            INTERNAL_URL, specialist, marketplace_service.id, working_day,
        ))
        assert r.status_code == 200, r.data
        assert r.json()["slots"], "expected slots for a working day"

    def test_salon_service_with_link_gives_slots(
        self, specialist, salon_link, working_day,
    ):
        salon = salon_link.salon_service
        r = _api().get(_slots_url(
            INTERNAL_URL, specialist, salon.id, working_day,
        ))
        assert r.status_code == 200, r.data
        slots = r.json()["slots"]
        assert slots, "expected slots via the salon fallback"
        for raw in slots:
            parsed = datetime.fromisoformat(raw)
            assert parsed.tzinfo is not None

    def test_salon_without_link_404(self, specialist, tenant, category, working_day):
        salon = SalonService.objects.create(
            tenant=tenant, category=category, name="Unlinked",
            duration_minutes=30,
        )
        r = _api().get(_slots_url(
            INTERNAL_URL, specialist, salon.id, working_day,
        ))
        assert r.status_code == 404

    def test_public_slots_salon_id_still_404(
        self, specialist, salon_link, working_day,
    ):
        """Public surface unchanged (AMD-019 bounds the fallback to the
        internal surface): a SalonService id on the PUBLIC endpoint
        behaves exactly as before — 404."""
        salon = salon_link.salon_service
        viewer = User.objects.create_user(
            username="amd019_viewer", password="x", role="client",
            phone="+79996190009",
        )
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=viewer)
        r = c.get(_slots_url(PUBLIC_URL, specialist, salon.id, working_day))
        assert r.status_code == 404


@pytest.mark.django_db
class TestSharedResolverPin:
    def test_slots_and_create_use_the_same_resolver(
        self, specialist, salon_link, working_day, customer=None,
    ):
        """Import/call-level pin: BOTH the internal slots helper and
        CreateBookingService resolve via
        services.service_resolver.resolve_bookable_service."""
        from services.service_resolver import ResolvedService
        from appointments.application.dto import CreateBookingDTO
        from appointments.application.services.create_booking_service import (
            CreateBookingService,
        )

        calls = []

        def spy(*, service_id, specialist, tenant=None):
            calls.append(str(service_id))
            return ResolvedService(
                kind="marketplace", service_id=service_id,
                name="Spy", duration_minutes=60, price=Decimal("100.00"),
            )

        salon = salon_link.salon_service
        from uuid import uuid4
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "services.service_resolver.resolve_bookable_service", spy,
            )
            # slots path
            from users.specialists_api import compute_specialist_day_slots
            payload, error = compute_specialist_day_slots(
                specialist,
                service_id=str(salon.id),
                date_param=working_day.isoformat(),
                allow_salon_fallback=True,
            )
            assert error is None
            # create path — validation only (the spy forces a
            # marketplace result so resolution passes; we only care
            # that the same resolver entry point was used).
            dto = CreateBookingDTO(
                client_id=uuid4(),
                specialist_id=specialist.id,
                service_id=salon.id,
                start_at=(
                    datetime.now(tz=timezone.utc) + timedelta(hours=3)
                ),
                idempotency_key=str(uuid4()),
            )
            CreateBookingService()._validate_pre_transaction(dto)

        assert calls.count(str(salon.id)) == 2  # one call per surface
