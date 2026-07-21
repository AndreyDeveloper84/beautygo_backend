"""AMD-019 — shared service_id resolver unit tests.

Resolution order: marketplace Service first (UUID collision priority),
then SalonService with an ACTIVE SpecialistService link in the current
tenant. Another tenant's rows are indistinguishable from missing ones.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from services.service_resolver import (
    ServiceUnavailableForSpecialistError,
    resolve_bookable_service,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="rsv-t", name="RSV Tenant")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(slug="rsv-other", name="RSV Other")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="rsv_spec", password="x", role="specialist",
        phone="+79996000001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="RSV Cat", slug="rsv-cat")


@pytest.fixture
def marketplace_service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="RSV Mkt",
        price=Decimal("1500.00"), duration_minutes=45, is_active=True,
        buffer_after_minutes=10,
    )


@pytest.fixture
def salon_service(tenant, category):
    return SalonService.objects.create(
        tenant=tenant, category=category, name="RSV Salon",
        duration_minutes=60, base_price=Decimal("2000.00"),
    )


@pytest.fixture
def salon_link(salon_service, specialist):
    return SpecialistService.objects.create(
        salon_service=salon_service, specialist=specialist,
        duration_minutes=None, price=Decimal("2200.00"),
        buffer_after_minutes=5, is_active=True,
    )


@pytest.mark.django_db
class TestMarketplaceBranch:
    def test_resolves_marketplace_fields(self, specialist, marketplace_service):
        r = resolve_bookable_service(
            service_id=marketplace_service.id, specialist=specialist,
        )
        assert r.kind == "marketplace"
        assert r.service_id == marketplace_service.id
        assert r.name == "RSV Mkt"
        assert r.duration_minutes == 45
        assert r.price == Decimal("1500.00")
        assert r.buffer_after_minutes == 10

    def test_inactive_marketplace_rejected(self, specialist, marketplace_service):
        marketplace_service.is_active = False
        marketplace_service.save()
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(
                service_id=marketplace_service.id, specialist=specialist,
            )

    def test_uuid_collision_marketplace_wins(
        self, specialist, salon_link, marketplace_service,
    ):
        """AMD-019: the same UUID existing in BOTH catalogs resolves to
        the marketplace Service."""
        collision_id = marketplace_service.id
        salon_service = salon_link.salon_service
        salon_service.id = collision_id
        salon_service.save()
        r = resolve_bookable_service(
            service_id=collision_id, specialist=specialist,
        )
        assert r.kind == "marketplace"


@pytest.mark.django_db
class TestSalonBranch:
    def test_resolves_salon_with_active_link(self, specialist, salon_link):
        salon = salon_link.salon_service
        r = resolve_bookable_service(
            service_id=salon.id, specialist=specialist,
        )
        assert r.kind == "salon"
        assert r.service_id == salon.id
        assert r.name == salon.name
        # AMD-019: duration from SalonService.duration_minutes…
        assert r.duration_minutes == 60
        # …price from the SpecialistService link
        assert r.price == Decimal("2200.00")
        assert r.buffer_after_minutes == 5

    def test_duration_falls_back_to_resolution_cascade(
        self, specialist, category, tenant,
    ):
        """salon.duration_minutes NULL → link resolution cascade
        (specialist → salon → template), never None for an active
        bookable."""
        template = ServiceTemplate.objects.create(
            category=category, name="RSV Tpl", name_short="Tpl",
            duration_default=90,
        )
        salon = SalonService.objects.create(
            tenant=tenant, template=template, name="RSV Untimed",
            duration_minutes=None,
        )
        SpecialistService.objects.create(
            salon_service=salon, specialist=specialist,
            duration_minutes=None, price=Decimal("1000.00"), is_active=True,
        )
        r = resolve_bookable_service(service_id=salon.id, specialist=specialist)
        assert r.duration_minutes == 90

    def test_no_link_rejected(self, specialist, salon_service):
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(
                service_id=salon_service.id, specialist=specialist,
            )

    def test_inactive_link_rejected(self, specialist, salon_link):
        salon_link.is_active = False
        salon_link.save()
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(
                service_id=salon_link.salon_service_id, specialist=specialist,
            )

    def test_inactive_salon_rejected(self, specialist, salon_link):
        salon = salon_link.salon_service
        salon.is_active = False
        salon.save()
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(service_id=salon.id, specialist=specialist)

    def test_other_tenant_invisible(self, specialist, salon_link, other_tenant):
        """Tenant isolation: the same salon under ANOTHER tenant context
        is the same error as a missing service — no existence leak."""
        salon = salon_link.salon_service
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(
                service_id=salon.id, specialist=specialist,
                tenant=other_tenant,
            )

    def test_salon_of_other_tenant_with_link_rejected(
        self, specialist, other_tenant, category,
    ):
        """A salon living in another tenant + a link to OUR specialist:
        still rejected — the salon itself is out of tenant scope."""
        foreign_salon = SalonService.objects.create(
            tenant=other_tenant, category=category, name="Foreign",
            duration_minutes=30,
        )
        SpecialistService.objects.create(
            salon_service=foreign_salon, specialist=specialist,
            duration_minutes=None, price=Decimal("500.00"), is_active=True,
        )
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(
                service_id=foreign_salon.id, specialist=specialist,
            )

    def test_unknown_service_rejected(self, specialist):
        from uuid import uuid4
        with pytest.raises(ServiceUnavailableForSpecialistError):
            resolve_bookable_service(service_id=uuid4(), specialist=specialist)
