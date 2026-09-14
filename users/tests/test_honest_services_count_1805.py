"""Честное число услуг у мастера (DRF-1805, M13; карта P41/P86).

``services_count`` в профиле мастера считал только legacy ``Service``: на
пилоте канон наполнен, легаси пуст — и мастер с назначенными
``SpecialistService`` видел «0 услуг». Теперь определение одно с витриной
(``annotate_catalog_services_count``): активные legacy + активные
канонические связки с активной ``SalonService``.

Проба объявлена здесь же: без правки первый тест красный (канон = 0).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from ai.tests.factories import make_specialist
from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.serializers import SpecialistProfileDetailSerializer


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="honest-count", name="Honest Count")


@pytest.fixture
def profile(db, tenant):
    profile = make_specialist(display_name="Ирина П.", address="Penza, Lenina 1")
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return profile


def _canonical(profile, tenant, *, name, salon_active=True, link_active=True):
    slug = f"hc-{uuid.uuid4().hex[:8]}"
    category = ServiceCategory.objects.create(name=f"cat {name}", slug=slug)
    template = ServiceTemplate.objects.create(
        category=category, name=f"{name} (канон)", name_short=name[:20], duration_default=60,
    )
    salon = SalonService.objects.create(
        tenant=tenant, category=category, template=template, name=name,
        duration_minutes=60, is_active=salon_active,
        mapping_status=SalonService.MappingStatus.VERIFIED,
        mapping_confirmed_rule="test_fixture", mapping_rule_version="1.0.0",
        mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture:test_1805",
    )
    return SpecialistService.objects.create(
        salon_service=salon, specialist=profile, price=Decimal("2000"),
        duration_minutes=60, is_active=link_active,
    )


def _count(profile) -> int:
    return SpecialistProfileDetailSerializer(profile).data["services_count"]


@pytest.mark.django_db
class TestHonestServicesCount:
    def test_canonical_links_are_counted(self, profile, tenant):
        """Форма пилота: легаси пуст, канон наполнен — число не ноль."""
        _canonical(profile, tenant, name="Массаж спины")
        _canonical(profile, tenant, name="Массаж стоп")
        assert Service.objects.filter(specialist=profile).count() == 0
        assert _count(profile) == 2

    def test_legacy_still_counted_and_both_layers_add_up(self, profile, tenant):
        Service.objects.create(
            specialist=profile, name="Маникюр", price="1500", duration_minutes=60,
        )
        _canonical(profile, tenant, name="Массаж спины")
        assert _count(profile) == 2

    def test_inactive_rows_are_not_counted(self, profile, tenant):
        """Как у витрины: неактивная legacy, неактивная связка и связка
        с неактивной SalonService — не услуги."""
        # Две неактивных legacy, а не одна: прежний счёт (legacy целиком)
        # дал бы 2 ≠ 1 — иначе тест зеленел бы на старом коде совпадением.
        for name in ("Старое", "Ещё старее"):
            Service.objects.create(
                specialist=profile, name=name, price="1", duration_minutes=10, is_active=False,
            )
        _canonical(profile, tenant, name="Снятая связка", link_active=False)
        _canonical(profile, tenant, name="Снятая услуга", salon_active=False)
        _canonical(profile, tenant, name="Живая")
        assert _count(profile) == 1

    def test_matches_the_storefront_definition(self, profile, tenant):
        """Одно определение: число из сериализатора равно
        ``active_services_count`` витрины на тех же строках."""
        from services.catalog_reads import annotate_catalog_services_count
        from users.models import SpecialistProfile

        Service.objects.create(
            specialist=profile, name="Маникюр", price="1500", duration_minutes=60,
        )
        _canonical(profile, tenant, name="Массаж спины")
        _canonical(profile, tenant, name="Снятая", link_active=False)
        annotated = annotate_catalog_services_count(
            SpecialistProfile.objects.filter(pk=profile.pk)
        ).get()
        assert _count(profile) == annotated.active_services_count == 2
