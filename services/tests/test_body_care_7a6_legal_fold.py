"""Body Care §7A-6: класс и лицензия в состоянии валидации CAT-6 (DRF-2842).

Решения главного окна 06.10: в состояние сворачиваются только гейты уровня
предложения (класс §7A-0, лицензия §7A-2); лицензия не проверена / канон
вне объёма → ``UNVERIFIED_LICENSE_STATE`` (BLOCKED по умолчанию, F-BC-016);
класс не подтверждён → REVIEW_REQUIRED; ``not_subject`` не меняется.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from services import body_care_validation
from services.body_care_validation import (
    BLOCKED,
    INCOMPLETE,
    NOT_SUBJECT,
    READY_FOR_SCREENING,
    RETIRED,
    REVIEW_REQUIRED,
    record_config_review,
    validation_state,
)
from services.models import OfferingConfigFact, SalonService, ServiceCategory, ServiceTemplate
from tenants.models import MedicalLicense, Tenant
from users.models import User

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State
Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass

WRAP = {
    F.PRODUCT_NAME: "Маска",
    F.INSTRUCTION_VERSION: "v3",
    F.APPLICATION_AREA: "тело",
    F.COVERING_TYPE: "плёнка",
    F.EXPOSURE_SECONDS: 1800,
    F.REMOVAL_METHOD: "душ",
    F.ADDITIONAL_MODALITY: "нет",
}


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="bc7a6-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="bc7a6-salon", name="Салон 7A-6")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-6", slug="bc-7a6")


def _offering(salon, category, staff, name, legal_class=None, family=Family.BODY_WRAP, **tpl) -> SalonService:
    template = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40],
        service_family=family, canonical_version="1" if family else "", **tpl,
    )
    if legal_class is not None:
        ServiceTemplate.objects.filter(pk=template.pk).update(
            legal_service_class=legal_class, legal_class_confirmed_by=staff,
            legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
        )
    return SalonService.objects.create(
        tenant=salon, category=category, template=template, name=name,
        duration_minutes=60, base_price=Decimal("3000"), configuration_version="cfg-1",
    )


def _ready_config(offering, staff) -> None:
    """Полная конфигурация и действующее ревью: всё, кроме §7A, готово."""
    for field, value in WRAP.items():
        OfferingConfigFact.objects.create(
            salon_service=offering, field=field, state=St.KNOWN, value=value,
            source_ref="инструкция", source_type="manufacturer_instruction",
        )
    record_config_review(offering, by=staff, source_ref="ревью куратора")


def _license(salon, staff, covers=()) -> MedicalLicense:
    lic = MedicalLicense.objects.create(tenant=salon, license_ref="ЛО-00-00-000006")
    MedicalLicense.objects.filter(pk=lic.pk).update(
        verified_by=staff, verified_at=timezone.now(), verification_source_ref="скан сверен"
    )
    lic.covered_templates.set(covers)
    return lic


# ─── класс ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("legal_class", [None, LC.LEGAL_REVIEW_REQUIRED])
def test_an_unconfirmed_class_keeps_a_ready_config_in_review(salon, category, staff, legal_class) -> None:
    ss = _offering(salon, category, staff, f"Обёртывание без класса {legal_class}", legal_class)
    _ready_config(ss, staff)

    assert validation_state(ss) == REVIEW_REQUIRED


def test_incomplete_outranks_an_unconfirmed_class(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Обёртывание неполное без класса")

    assert validation_state(ss) == INCOMPLETE


def test_a_confirmed_non_medical_class_does_not_interfere(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Обёртывание немедицинское", LC.NON_MEDICAL_COSMETIC)
    _ready_config(ss, staff)

    assert validation_state(ss) == READY_FOR_SCREENING


# ─── лицензия ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("legal_class", [LC.MEDICAL_COSMETOLOGY, LC.MEDICAL_OTHER])
def test_a_medical_class_without_a_license_is_blocked(salon, category, staff, legal_class) -> None:
    ss = _offering(salon, category, staff, f"Медицинское без лицензии {legal_class}", legal_class)
    _ready_config(ss, staff)

    assert validation_state(ss) == BLOCKED


def test_a_canon_outside_the_license_scope_is_blocked(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Медицинское вне объёма", LC.MEDICAL_COSMETOLOGY)
    other = _offering(salon, category, staff, "Покрытая процедура", LC.MEDICAL_COSMETOLOGY)
    _ready_config(ss, staff)
    _license(salon, staff, covers=[other.template])

    assert validation_state(ss) == BLOCKED


def test_a_blocked_license_outranks_incomplete(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Медицинское неполное", LC.MEDICAL_COSMETOLOGY)

    assert validation_state(ss) == BLOCKED


def test_a_covering_verified_license_lets_a_ready_config_be_ready(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Медицинское с лицензией", LC.MEDICAL_COSMETOLOGY)
    _ready_config(ss, staff)
    _license(salon, staff, covers=[ss.template])

    assert validation_state(ss) == READY_FOR_SCREENING


def test_retired_still_outranks_the_license(salon, category, staff) -> None:
    ss = _offering(
        salon, category, staff, "Медицинское выведенное", LC.MEDICAL_COSMETOLOGY,
        lifecycle=ServiceTemplate.Lifecycle.RETIRED, retired_by=staff,
        retired_at=timezone.now(), retirement_source_ref="реестр",
    )

    assert validation_state(ss) == RETIRED


# ─── переключатель A2(3) ────────────────────────────────────────────────────


def test_the_default_for_an_unverified_license_is_blocked() -> None:
    assert body_care_validation.UNVERIFIED_LICENSE_STATE == BLOCKED


def test_switching_to_review_moves_it_below_incomplete(salon, category, staff, monkeypatch) -> None:
    """Если владелец ответит A2(3) = REVIEW: ready-конфигурация → REVIEW_REQUIRED,
    неполная → INCOMPLETE (REVIEW ниже INCOMPLETE в лестнице)."""
    monkeypatch.setattr(body_care_validation, "UNVERIFIED_LICENSE_STATE", REVIEW_REQUIRED)
    ready = _offering(salon, category, staff, "Медицинское ревью", LC.MEDICAL_COSMETOLOGY)
    _ready_config(ready, staff)
    bare = _offering(salon, category, staff, "Медицинское ревью неполное", LC.MEDICAL_COSMETOLOGY)

    assert validation_state(ready) == REVIEW_REQUIRED
    assert validation_state(bare) == INCOMPLETE


# ─── контракт CAT-10 ────────────────────────────────────────────────────────


def test_a_medical_class_outside_body_care_stays_not_subject(salon, category, staff) -> None:
    """Пилинг лица: гейтится отдельно (DRF-2843), CAT-6 его не сворачивает."""
    ss = _offering(salon, category, staff, "Пилинг лица", LC.MEDICAL_COSMETOLOGY, family=None)

    assert validation_state(ss) == NOT_SUBJECT
