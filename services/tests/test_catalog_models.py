"""S3A catalog domain models — TDD (spec: docs/CATALOG_DOMAIN_REBUILD_S3_DESIGN_2026-07.md).

New additive layer: SalonService -> SpecialistService (bookable), plus
DraftSalonService + ExternalSourceMapping. Service/Appointment untouched.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from services.models import (
    DraftSalonService,
    ExternalSourceMapping,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="penza-salon", name="Penza Salon")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Маникюр S3")


@pytest.fixture
def template(category):
    return ServiceTemplate.objects.create(
        category=category,
        name="Классический маникюр",
        name_short="Маникюр",
        duration_default=60,
        requires_health_check=False,
    )


@pytest.fixture
def salon_service(tenant, template, category):
    return SalonService.objects.create(
        tenant=tenant, template=template, category=category,
        name="Классический маникюр",
    )


# --------------------------------------------------------------------------- #
# SalonService
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestSalonService:
    def test_create_from_template(self, tenant, template, category):
        ss = SalonService.objects.create(
            tenant=tenant, template=template, category=category,
            name="Классический маникюр",
        )
        assert ss.id is not None
        assert ss.is_active is True
        assert ss.source == "manual"

    def test_requires_category_when_no_template(self, tenant):
        ss = SalonService(tenant=tenant, template=None, category=None, name="Кастом")
        with pytest.raises(ValidationError):
            ss.save()

    def test_custom_service_allowed_with_category(self, tenant, category):
        ss = SalonService.objects.create(
            tenant=tenant, template=None, category=category, name="Кастомная услуга",
        )
        assert ss.template_id is None
        assert ss.category_id == category.id

    def test_unique_tenant_template_name(self, tenant, template, category):
        SalonService.objects.create(
            tenant=tenant, template=template, category=category, name="Дубль",
        )
        with pytest.raises(IntegrityError):
            SalonService.objects.create(
                tenant=tenant, template=template, category=category, name="Дубль",
            )


# --------------------------------------------------------------------------- #
# SpecialistService (bookable)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestSpecialistService:
    def test_bookable_create(self, salon_service, specialist_user):
        sp = SpecialistService.objects.create(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=45, price=Decimal("1500"),
        )
        assert sp.id is not None
        assert sp.is_active is True

    def test_tenant_denormalized_from_salon_service(self, salon_service, specialist_user):
        sp = SpecialistService.objects.create(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=45, price=Decimal("1500"),
        )
        assert sp.tenant_id == salon_service.tenant_id

    def test_resolved_duration_prefers_own(self, salon_service, specialist_user):
        sp = SpecialistService.objects.create(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=30, price=Decimal("1000"),
        )
        assert sp.resolved_duration() == 30

    def test_resolved_duration_falls_back_to_salon(self, tenant, template, category, specialist_user):
        salon = SalonService.objects.create(
            tenant=tenant, template=template, category=category,
            name="С длительностью салона", duration_minutes=90,
        )
        sp = SpecialistService.objects.create(
            salon_service=salon, specialist=specialist_user.specialist_profile,
            duration_minutes=90, price=Decimal("1000"),
        )
        sp.duration_minutes = None  # simulate unset override
        assert sp.resolved_duration() == 90

    def test_resolved_duration_falls_back_to_template(self, salon_service, specialist_user):
        sp = SpecialistService(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=None, price=Decimal("1000"),
        )
        # salon_service has no own duration -> template.duration_default (60)
        assert sp.resolved_duration() == 60

    def test_active_bookable_requires_resolvable_duration(self, tenant, category, specialist_user):
        # template with no duration_default -> nothing resolves
        bare_template = ServiceTemplate.objects.create(
            category=category, name="Без длительности", name_short="Без",
            duration_default=None,
        )
        salon = SalonService.objects.create(
            tenant=tenant, template=bare_template, category=category,
            name="Без длительности", duration_minutes=None,
        )
        sp = SpecialistService(
            salon_service=salon, specialist=specialist_user.specialist_profile,
            duration_minutes=None, price=Decimal("1000"), is_active=True,
        )
        with pytest.raises(ValidationError):
            sp.save()

    def test_health_check_escalate_only_from_template_floor(self, tenant, category, specialist_user):
        gated_template = ServiceTemplate.objects.create(
            category=category, name="Гейтед", name_short="Гейт",
            duration_default=60, requires_health_check=True,
        )
        salon = SalonService.objects.create(
            tenant=tenant, template=gated_template, category=category,
            name="Гейтед", requires_health_check=False,
        )
        sp = SpecialistService.objects.create(
            salon_service=salon, specialist=specialist_user.specialist_profile,
            duration_minutes=60, price=Decimal("2000"),
            requires_health_check=False,  # cannot relax the template floor
        )
        assert sp.resolved_requires_health_check() is True

    def test_health_check_escalates_from_specialist(self, salon_service, specialist_user):
        # template floor False, specialist escalates to True
        sp = SpecialistService.objects.create(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=60, price=Decimal("2000"),
            requires_health_check=True,
        )
        assert sp.resolved_requires_health_check() is True

    def test_unique_specialist_salon_service(self, salon_service, specialist_user):
        SpecialistService.objects.create(
            salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
            duration_minutes=60, price=Decimal("1000"),
        )
        with pytest.raises(IntegrityError):
            SpecialistService.objects.create(
                salon_service=salon_service,
                specialist=specialist_user.specialist_profile,
                duration_minutes=60, price=Decimal("1200"),
            )


# --------------------------------------------------------------------------- #
# ExternalSourceMapping (idempotent external<->Ayla key)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestExternalSourceMapping:
    def test_service_mapping(self, tenant, salon_service):
        m = ExternalSourceMapping.objects.create(
            source="yclients", external_type="service", external_id="777",
            tenant=tenant, salon_service=salon_service,
        )
        assert m.id is not None

    def test_staff_mapping(self, tenant, specialist_user):
        m = ExternalSourceMapping.objects.create(
            source="yclients", external_type="staff", external_id="42",
            tenant=tenant, specialist=specialist_user.specialist_profile,
        )
        assert m.id is not None

    def test_idempotency_unique(self, tenant, salon_service):
        ExternalSourceMapping.objects.create(
            source="yclients", external_type="service", external_id="777",
            tenant=tenant, salon_service=salon_service,
        )
        with pytest.raises(IntegrityError):
            ExternalSourceMapping.objects.create(
                source="yclients", external_type="service", external_id="777",
                tenant=tenant, salon_service=salon_service,
            )

    def test_service_type_requires_salon_service(self, tenant, specialist_user):
        m = ExternalSourceMapping(
            source="yclients", external_type="service", external_id="1",
            tenant=tenant, specialist=specialist_user.specialist_profile,
        )
        with pytest.raises(ValidationError):
            m.save()

    def test_staff_type_requires_specialist(self, tenant, salon_service):
        m = ExternalSourceMapping(
            source="yclients", external_type="staff", external_id="1",
            tenant=tenant, salon_service=salon_service,
        )
        with pytest.raises(ValidationError):
            m.save()

    def test_rejects_both_targets(self, tenant, salon_service, specialist_user):
        m = ExternalSourceMapping(
            source="yclients", external_type="service", external_id="1",
            tenant=tenant, salon_service=salon_service,
            specialist=specialist_user.specialist_profile,
        )
        with pytest.raises(ValidationError):
            m.save()


# --------------------------------------------------------------------------- #
# DraftSalonService ("Confirm, don't create")
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestDraftSalonService:
    def test_defaults_pending(self, tenant):
        d = DraftSalonService.objects.create(
            tenant=tenant, external_name="Маникюр из YClients",
            external_service_id="555",
        )
        assert d.status == "pending"
        assert d.external_source == "yclients"
        assert d.raw_payload == {}

    def test_conditional_unique_on_external_id(self, tenant):
        DraftSalonService.objects.create(
            tenant=tenant, external_name="A", external_service_id="555",
        )
        with pytest.raises(IntegrityError):
            DraftSalonService.objects.create(
                tenant=tenant, external_name="B", external_service_id="555",
            )

    def test_allows_multiple_blank_external_id(self, tenant):
        DraftSalonService.objects.create(
            tenant=tenant, external_name="Manual 1", external_service_id="",
        )
        # second blank draft must NOT collide
        DraftSalonService.objects.create(
            tenant=tenant, external_name="Manual 2", external_service_id="",
        )
        assert DraftSalonService.objects.filter(external_service_id="").count() == 2
