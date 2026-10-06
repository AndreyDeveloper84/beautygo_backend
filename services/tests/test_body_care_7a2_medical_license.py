"""Body Care §7A-2: медицинская лицензия салона и выводимое состояние лицензии (DRF-2838).

Shape одобрен главным окном 06.10: лицензия — сущность салона, проверка —
провенанс человека («все три или ни одного»), объём — каноны (fail-closed),
места — только этого салона; состояние предложения выводится и читает
только ПОДТВЕРЖДЁННЫЙ класс §7A-0; ``medical_other`` требует лицензию сверх
буквы §7A.3 (fail-closed, ждёт D-1).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.body_care_license import (
    CLASS_UNCONFIRMED,
    CODES,
    LICENSE_STATES,
    NOT_REQUIRED,
    NOT_VERIFIED,
    SCOPE_MISMATCH,
    VERIFIED,
    license_state,
    license_states,
)
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import MedicalLicense, ServiceLocation, Tenant
from users.models import User

pytestmark = pytest.mark.django_db

Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass


@pytest.fixture
def lawyer(db):
    return User.objects.create_user(username="bc7a2-lawyer", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="bc7a2-salon", name="Салон 7A-2")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="bc7a2-other", name="Другой салон 7A-2")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-2", slug="bc-7a2")


def _canon(category, lawyer, name, legal_class=None, family=Family.BODY_WRAP) -> ServiceTemplate:
    tpl = ServiceTemplate.objects.create(
        category=category,
        name=name,
        name_short=name[:40],
        service_family=family,
        canonical_version="1" if family else "",
    )
    if legal_class is not None:
        ServiceTemplate.objects.filter(pk=tpl.pk).update(
            legal_service_class=legal_class,
            legal_class_confirmed_by=lawyer,
            legal_class_confirmed_at=timezone.now(),
            legal_class_source_ref="решение юриста",
        )
    return tpl


def _offering(salon, category, template) -> SalonService:
    return SalonService.objects.create(
        tenant=salon,
        category=category,
        template=template,
        name=template.name,
        duration_minutes=60,
        base_price=Decimal("3000"),
    )


def _license(salon, lawyer=None, ref="ЛО-00-00-000001", covers=()) -> MedicalLicense:
    lic = MedicalLicense.objects.create(tenant=salon, license_ref=ref)
    if lawyer is not None:
        MedicalLicense.objects.filter(pk=lic.pk).update(
            verified_by=lawyer, verified_at=timezone.now(), verification_source_ref="скан, сверено"
        )
    lic.covered_templates.set(covers)
    return lic


# ─── модель лицензии ─────────────────────────────────────────────────────────


def test_a_license_without_a_number_is_refused(salon) -> None:
    with pytest.raises(IntegrityError, match="medicallicense_ref_required"):
        with transaction.atomic():
            MedicalLicense.objects.create(tenant=salon, license_ref="")


def test_the_number_is_unique_per_salon(salon) -> None:
    _license(salon)

    with pytest.raises(IntegrityError, match="medicallicense_unique_ref_per_tenant"):
        with transaction.atomic():
            _license(salon)


@pytest.mark.parametrize("present", ["by", "at", "source_ref", "by+at", "at+source_ref"])
def test_a_partial_verification_is_refused(salon, lawyer, present) -> None:
    lic = _license(salon)
    fields = {}
    if "by" in present:
        fields["verified_by"] = lawyer
    if "at" in present:
        fields["verified_at"] = timezone.now()
    if "source_ref" in present:
        fields["verification_source_ref"] = "скан"

    with pytest.raises(IntegrityError, match="medicallicense_verification_all_or_nothing"):
        with transaction.atomic():
            MedicalLicense.objects.filter(pk=lic.pk).update(**fields)


def test_a_full_verification_is_accepted(salon, lawyer) -> None:
    lic = _license(salon, lawyer)
    lic.refresh_from_db()

    assert lic.is_verified


def test_a_location_of_the_licensee_is_accepted(salon) -> None:
    lic = _license(salon)
    place = ServiceLocation.objects.create(tenant=salon, address="ул. Тестовая, 1")

    lic.licensed_locations.add(place)

    assert list(lic.licensed_locations.all()) == [place]


def test_a_location_of_another_salon_is_refused(salon, other_salon) -> None:
    lic = _license(salon)
    foreign = ServiceLocation.objects.create(tenant=other_salon, address="ул. Чужая, 2")

    with pytest.raises(ValidationError, match="салону-лицензиату"):
        lic.licensed_locations.add(foreign)


def test_a_location_without_a_salon_is_refused(salon) -> None:
    lic = _license(salon)
    solo = ServiceLocation.objects.create(tenant=None, address="Кабинет соло-мастера")

    with pytest.raises(ValidationError, match="салону-лицензиату"):
        lic.licensed_locations.add(solo)


def test_the_reverse_side_is_guarded_too(salon, other_salon) -> None:
    lic = _license(salon)
    foreign = ServiceLocation.objects.create(tenant=other_salon, address="ул. Чужая, 3")

    with pytest.raises(ValidationError, match="салону-лицензиату"):
        foreign.medical_licenses.add(lic)


def test_the_erasure_census_decides_the_verifier() -> None:
    from users.deletion_executor import RETAIN

    assert "tenants.MedicalLicense.verified_by" in RETAIN


# ─── выводимое состояние ────────────────────────────────────────────────────


def test_the_states_and_codes() -> None:
    assert set(LICENSE_STATES) == {
        "not_required", "class_unconfirmed", "not_verified", "scope_mismatch", "verified",
    }
    assert CODES == {
        "not_verified": "MEDICAL_LICENSE_NOT_VERIFIED",
        "scope_mismatch": "LICENSE_SCOPE_MISMATCH",
    }


def test_a_confirmed_non_medical_class_needs_no_license(salon, category, lawyer) -> None:
    wrap = _canon(category, lawyer, "Обёртывание немедицинское", LC.NON_MEDICAL_COSMETIC)

    assert license_state(_offering(salon, category, wrap)) == NOT_REQUIRED


def test_a_canon_outside_body_care_without_a_class_needs_no_license(salon, category, lawyer) -> None:
    haircut = _canon(category, lawyer, "Стрижка", family=None)

    assert license_state(_offering(salon, category, haircut)) == NOT_REQUIRED


def test_an_unconfirmed_class_is_not_read_from_the_candidate(salon, category, lawyer) -> None:
    """Кандидат §7A-1 для обёртывания — немедицинский; но без подтверждения
    человеком класс не установлен, и гейт это не открывает."""
    wrap = _canon(category, lawyer, "Обёртывание без класса")

    assert license_state(_offering(salon, category, wrap)) == CLASS_UNCONFIRMED


def test_legal_review_required_is_unconfirmed(salon, category, lawyer) -> None:
    wrap = _canon(category, lawyer, "Обёртывание на проверке", LC.LEGAL_REVIEW_REQUIRED)

    assert license_state(_offering(salon, category, wrap)) == CLASS_UNCONFIRMED


@pytest.mark.parametrize("legal_class", [LC.MEDICAL_COSMETOLOGY, LC.MEDICAL_OTHER])
def test_a_medical_class_without_a_license_is_not_verified(salon, category, lawyer, legal_class) -> None:
    """``medical_other`` — сверх буквы §7A.3, fail-closed (ждёт D-1)."""
    peel = _canon(category, lawyer, f"Медицинская {legal_class}", legal_class)

    assert license_state(_offering(salon, category, peel)) == NOT_VERIFIED


def test_an_unverified_license_does_not_count(salon, category, lawyer) -> None:
    peel = _canon(category, lawyer, "Пилинг непроверенный", LC.MEDICAL_COSMETOLOGY)
    _license(salon, lawyer=None, covers=[peel])

    assert license_state(_offering(salon, category, peel)) == NOT_VERIFIED


def test_a_verified_license_outside_its_scope_is_a_mismatch(salon, category, lawyer) -> None:
    peel = _canon(category, lawyer, "Пилинг вне объёма", LC.MEDICAL_COSMETOLOGY)
    other = _canon(category, lawyer, "Другая процедура", LC.MEDICAL_COSMETOLOGY)
    _license(salon, lawyer, covers=[other])

    assert license_state(_offering(salon, category, peel)) == SCOPE_MISMATCH


def test_a_verified_license_with_an_empty_scope_covers_nothing(salon, category, lawyer) -> None:
    peel = _canon(category, lawyer, "Пилинг пустой объём", LC.MEDICAL_COSMETOLOGY)
    _license(salon, lawyer)

    assert license_state(_offering(salon, category, peel)) == SCOPE_MISMATCH


def test_a_verified_license_covering_the_canon_is_verified(salon, category, lawyer) -> None:
    peel = _canon(category, lawyer, "Пилинг покрытый", LC.MEDICAL_COSMETOLOGY)
    _license(salon, lawyer, covers=[peel])

    assert license_state(_offering(salon, category, peel)) == VERIFIED


def test_another_salons_license_does_not_count(salon, other_salon, category, lawyer) -> None:
    peel = _canon(category, lawyer, "Пилинг чужая лицензия", LC.MEDICAL_COSMETOLOGY)
    _license(other_salon, lawyer, covers=[peel])

    assert license_state(_offering(salon, category, peel)) == NOT_VERIFIED


def test_a_medical_class_outside_body_care_is_still_checked(salon, category, lawyer) -> None:
    """«Вне body-care» не отменяет подтверждённого медицинского класса."""
    face_peel = _canon(category, lawyer, "Пилинг лица", LC.MEDICAL_COSMETOLOGY, family=None)

    assert license_state(_offering(salon, category, face_peel)) == NOT_VERIFIED


def test_the_pool_is_read_in_two_queries(salon, category, lawyer, django_assert_num_queries) -> None:
    peel = _canon(category, lawyer, "Пул пилинг", LC.MEDICAL_COSMETOLOGY)
    wrap = _canon(category, lawyer, "Пул обёртывание", LC.NON_MEDICAL_COSMETIC)
    bare = _canon(category, lawyer, "Пул без класса")
    _license(salon, lawyer, covers=[peel])
    ids = [_offering(salon, category, t).pk for t in (peel, wrap, bare)]

    with django_assert_num_queries(2):
        result = license_states(ids)

    assert [result[i] for i in ids] == [VERIFIED, NOT_REQUIRED, CLASS_UNCONFIRMED]


def test_an_unknown_id_is_left_out(salon, category, lawyer) -> None:
    offering = _offering(salon, category, _canon(category, lawyer, "Одно"))

    assert set(license_states([offering.pk, uuid.uuid4()])) == {offering.pk}
