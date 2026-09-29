"""DRF-2614 — у флага гейта здоровья появилось происхождение.

`ServiceTemplate.requires_health_check` был двузначным флагом без единого поля
происхождения: 102 гейтованных шаблона засева выведены правилом (членство в
подкатегории, слово в названии), и различить черновой флаг и просмотренный
было нечем. Решение владельца §95 («черновой гейт не требует скрининга») было
неисполнимо.

Этот лист — только РАЗЛИЧЕНИЕ. Гейт записи по-прежнему отказывает по
поднятому флагу любого происхождения; поменять это — слово владельца.
Узлы — на пары, которые обязаны различаться: «гейт отказывает при True»
прошёл бы и при выведенном, и при подтверждённом флаге.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from appointments.application.services._booking_guards import check_health_screening
from appointments.domain.exceptions import HealthScreeningRequiredError
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from services.service_resolver import ResolvedService
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

Origin = ServiceTemplate.HealthCheckOrigin


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="salon-2614", name="Salon 2614")


@pytest.fixture
def specialist_user(specialist_user, tenant):
    profile = specialist_user.specialist_profile
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return specialist_user


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Инъекции 2614")


@pytest.fixture
def curator():
    return get_user_model().objects.create_user(username="curator-2614", password="x")


def _edge(tenant, category, specialist_user, *, name, template_flag=True, origin=None, **tpl):
    template = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:20], duration_default=60,
        requires_health_check=template_flag, **(origin or {}), **tpl,
    )
    salon = SalonService.objects.create(
        tenant=tenant, template=template, category=category, name=name,
    )
    return SpecialistService.objects.create(
        salon_service=salon, specialist=specialist_user.specialist_profile,
        duration_minutes=60, price=Decimal("2000"),
    )


def _confirmed(curator) -> dict:
    return {
        "health_check_origin": Origin.CONFIRMED,
        "health_check_confirmed_by": curator,
        "health_check_confirmed_at": timezone.now(),
        "health_check_source_ref": "owner-review-2614",
    }


def _resolved(edge) -> ResolvedService:
    verdict, basis = edge.resolved_health_check()
    return ResolvedService(
        kind="salon", service_id=edge.salon_service_id, name="x",
        duration_minutes=60, price=Decimal("2000"),
        requires_health_check=verdict, health_check_basis=basis,
    )


class TestInferredAndConfirmedAreDistinguishableInTheGate:
    def test_an_inferred_and_a_confirmed_floor_refuse_with_different_bases(
        self, tenant, category, specialist_user, curator
    ) -> None:
        inferred = _edge(tenant, category, specialist_user, name="Выведенный гейт")
        confirmed = _edge(
            tenant, category, specialist_user, name="Подтверждённый гейт",
            origin=_confirmed(curator),
        )

        refusals = []
        for edge in (inferred, confirmed):
            with pytest.raises(HealthScreeningRequiredError) as caught:
                check_health_screening(_resolved(edge))
            refusals.append((caught.value.reason, caught.value.basis))

        # Решение гейта прежнее — отказ REQUIRED в обоих случаях…
        assert [r for r, _ in refusals] == [HealthScreeningRequiredError.REQUIRED] * 2
        # …но состояния различимы: черновой пол против просмотренного.
        assert [b for _, b in refusals] == ["template_inferred", "template_confirmed"]

    def test_the_old_verdict_method_is_unchanged(
        self, tenant, category, specialist_user, curator
    ) -> None:
        inferred = _edge(tenant, category, specialist_user, name="Черновик")
        confirmed = _edge(
            tenant, category, specialist_user, name="Проверено", origin=_confirmed(curator)
        )
        assert inferred.resolved_requires_health_check() is True
        assert confirmed.resolved_requires_health_check() is True


class TestUnknownIsStillNotPermission:
    def test_no_template_and_nobody_answered_is_refused_as_unknown(
        self, tenant, category, specialist_user
    ) -> None:
        salon = SalonService.objects.create(
            tenant=tenant, template=None, category=category, name="Без шаблона",
            duration_minutes=60,
        )
        edge = SpecialistService.objects.create(
            salon_service=salon, specialist=specialist_user.specialist_profile,
            duration_minutes=60, price=Decimal("2000"),
        )
        # Положительная сторона: ребро с подтверждённым «нет» проходит.
        answered = _edge(tenant, category, specialist_user, name="Без гейта", template_flag=False)
        check_health_screening(_resolved(answered))

        with pytest.raises(HealthScreeningRequiredError) as caught:
            check_health_screening(_resolved(edge))

        assert caught.value.reason == HealthScreeningRequiredError.UNKNOWN
        assert caught.value.basis == "unknown"


class TestConfirmationNeedsProvenance:
    """То же устройство, что у одобрения строки (`servicetemplate_approved_*`)."""

    def test_a_confirmed_flag_by_a_person_or_by_a_named_rule_is_stored(
        self, category, curator
    ) -> None:
        by_person = ServiceTemplate.objects.create(
            category=category, name="Человек", name_short="Ч", **_confirmed(curator)
        )
        by_rule = ServiceTemplate.objects.create(
            category=category, name="Правило", name_short="П",
            health_check_origin=Origin.CONFIRMED,
            health_check_confirmed_rule="owner_health_review",
            health_check_rule_version="1",
            health_check_confirmed_at=timezone.now(),
            health_check_source_ref="owner-2614",
        )
        assert {by_person.health_check_origin, by_rule.health_check_origin} == {Origin.CONFIRMED}

    @pytest.mark.parametrize(
        "broken",
        ["no_author", "no_date", "no_source", "both_author_and_rule", "rule_without_version"],
    )
    def test_a_confirmation_without_proper_provenance_is_refused(
        self, category, curator, broken
    ) -> None:
        fields = _confirmed(curator)
        if broken == "no_author":
            fields["health_check_confirmed_by"] = None
        elif broken == "no_date":
            fields["health_check_confirmed_at"] = None
        elif broken == "no_source":
            fields["health_check_source_ref"] = ""
        elif broken == "both_author_and_rule":
            fields.update(health_check_confirmed_rule="r", health_check_rule_version="1")
        else:
            fields.update(
                health_check_confirmed_by=None, health_check_confirmed_rule="r",
                health_check_rule_version="",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            ServiceTemplate.objects.create(
                category=category, name=f"Сломано {broken}", name_short="С", **fields
            )

    def test_a_new_template_starts_inferred(self, category) -> None:
        row = ServiceTemplate.objects.create(category=category, name="Новый", name_short="Н")
        assert row.health_check_origin == Origin.INFERRED
