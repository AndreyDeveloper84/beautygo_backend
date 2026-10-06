"""Body Care §7A-3: адрес — подтверждённое место мастера в лицензии салона (DRF-2841).

Shape одобрен главным окном 06.10: по паре (мастер, предложение); место
должно быть подтверждено (§9) и указано в проверенной лицензии того же
салона, ПОКРЫВАЮЩЕЙ канон; читаются только подтверждённые классы §7A-0.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from services.body_care_address import (
    ADDRESS_MISMATCH,
    ADDRESS_STATES,
    CLASS_UNCONFIRMED,
    CODES,
    LOCATION_UNKNOWN,
    NO_COVERING_LICENSE,
    NOT_REQUIRED,
    VERIFIED,
    address_state,
    address_states,
)
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import MedicalLicense, ServiceLocation, Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="bc7a3-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="bc7a3-salon", name="Салон 7A-3")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-3", slug="bc-7a3")


def _place(salon, staff, address, confirmed=True) -> ServiceLocation:
    if not confirmed:
        return ServiceLocation.objects.create(tenant=salon, address=address)
    return ServiceLocation.objects.create(
        tenant=salon, address=address, status="confirmed",
        confirmed_by=staff, confirmed_at=timezone.now(), confirmed_source_ref="договор аренды",
    )


def _master(name, place=None) -> SpecialistProfile:
    user = User.objects.create_user(username=f"bc7a3-{name}", password="x")
    return SpecialistProfile.objects.create(user=user, display_name=name, works_at=place)


def _offering(salon, category, staff, name, legal_class=None, family=Family.BODY_WRAP) -> SalonService:
    tpl = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40],
        service_family=family, canonical_version="1" if family else "",
    )
    if legal_class is not None:
        ServiceTemplate.objects.filter(pk=tpl.pk).update(
            legal_service_class=legal_class, legal_class_confirmed_by=staff,
            legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
        )
    return SalonService.objects.create(
        tenant=salon, category=category, template=tpl, name=name,
        duration_minutes=60, base_price=Decimal("3000"),
    )


def _license(salon, staff=None, ref="ЛО-00-00-000003", covers=(), places=()) -> MedicalLicense:
    lic = MedicalLicense.objects.create(tenant=salon, license_ref=ref)
    if staff is not None:
        MedicalLicense.objects.filter(pk=lic.pk).update(
            verified_by=staff, verified_at=timezone.now(), verification_source_ref="скан сверен"
        )
    lic.covered_templates.set(covers)
    lic.licensed_locations.set(places)
    return lic


def test_the_states_and_codes() -> None:
    assert set(ADDRESS_STATES) == {
        "not_required", "class_unconfirmed", "location_unknown",
        "no_covering_license", "address_mismatch", "verified",
    }
    assert CODES == {"address_mismatch": "LICENSE_ADDRESS_MISMATCH"}


# ─── не медицинское ─────────────────────────────────────────────────────────


def test_a_non_medical_class_needs_no_address_check(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Обёртывание", LC.NON_MEDICAL_COSMETIC)

    assert address_state(_master("m1").pk, ss.pk) == NOT_REQUIRED


def test_no_family_and_no_class_is_not_required(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Стрижка", family=None)

    assert address_state(_master("m2").pk, ss.pk) == NOT_REQUIRED


@pytest.mark.parametrize("legal_class", [None, LC.LEGAL_REVIEW_REQUIRED])
def test_an_unconfirmed_class_is_unconfirmed(salon, category, staff, legal_class) -> None:
    ss = _offering(salon, category, staff, f"Неподтверждённый {legal_class}", legal_class)

    assert address_state(_master(f"m3{legal_class}").pk, ss.pk) == CLASS_UNCONFIRMED


# ─── место мастера ──────────────────────────────────────────────────────────


def test_a_master_without_a_place_is_unknown(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Пилинг без места", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, covers=[ss.template])

    assert address_state(_master("m4").pk, ss.pk) == LOCATION_UNKNOWN


def test_an_unconfirmed_place_is_unknown_even_if_licensed(salon, category, staff) -> None:
    """§9: неподтверждённое место — происхождение неизвестно; в лицензии оно или нет."""
    place = _place(salon, staff, "ул. Неподтверждённая, 1", confirmed=False)
    ss = _offering(salon, category, staff, "Пилинг неподтверждённое место", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, covers=[ss.template], places=[place])

    assert address_state(_master("m5", place).pk, ss.pk) == LOCATION_UNKNOWN


# ─── лицензия и адрес ───────────────────────────────────────────────────────


def test_no_license_means_no_covering_license(salon, category, staff) -> None:
    place = _place(salon, staff, "ул. Без лицензии, 2")
    ss = _offering(salon, category, staff, "Пилинг без лицензии", LC.MEDICAL_COSMETOLOGY)

    assert address_state(_master("m6", place).pk, ss.pk) == NO_COVERING_LICENSE


def test_an_unverified_license_does_not_cover(salon, category, staff) -> None:
    place = _place(salon, staff, "ул. Непроверенная, 3")
    ss = _offering(salon, category, staff, "Пилинг непроверенная", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff=None, covers=[ss.template], places=[place])

    assert address_state(_master("m7", place).pk, ss.pk) == NO_COVERING_LICENSE


def test_a_license_for_another_canon_does_not_cover(salon, category, staff) -> None:
    place = _place(salon, staff, "ул. Другой канон, 4")
    ss = _offering(salon, category, staff, "Пилинг вне объёма", LC.MEDICAL_COSMETOLOGY)
    other = _offering(salon, category, staff, "Другая процедура", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, covers=[other.template], places=[place])

    assert address_state(_master("m8", place).pk, ss.pk) == NO_COVERING_LICENSE


def test_a_place_outside_the_covering_license_is_a_mismatch(salon, category, staff) -> None:
    place = _place(salon, staff, "ул. Мимо лицензии, 5")
    licensed = _place(salon, staff, "ул. В лицензии, 6")
    ss = _offering(salon, category, staff, "Пилинг не по адресу", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, covers=[ss.template], places=[licensed])

    assert address_state(_master("m9", place).pk, ss.pk) == ADDRESS_MISMATCH


def test_the_place_must_be_in_the_license_that_covers_the_canon(salon, category, staff) -> None:
    """Место есть в одной лицензии салона, канон — в другой: адрес не совпал."""
    place = _place(salon, staff, "ул. Две лицензии, 7")
    ss = _offering(salon, category, staff, "Пилинг две лицензии", LC.MEDICAL_COSMETOLOGY)
    other = _offering(salon, category, staff, "Процедура второй лицензии", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, ref="ЛО-1", covers=[ss.template])
    _license(salon, staff, ref="ЛО-2", covers=[other.template], places=[place])

    assert address_state(_master("m10", place).pk, ss.pk) == ADDRESS_MISMATCH


@pytest.mark.parametrize("legal_class", [LC.MEDICAL_COSMETOLOGY, LC.MEDICAL_OTHER])
def test_a_confirmed_place_in_the_covering_license_is_verified(salon, category, staff, legal_class) -> None:
    place = _place(salon, staff, f"ул. Совпало, {legal_class}")
    ss = _offering(salon, category, staff, f"Пилинг по адресу {legal_class}", legal_class)
    _license(salon, staff, covers=[ss.template], places=[place])

    assert address_state(_master(f"m11{legal_class}", place).pk, ss.pk) == VERIFIED


def test_the_answer_is_per_master(salon, category, staff) -> None:
    """Одна услуга, два мастера в разных местах — разные ответы."""
    licensed = _place(salon, staff, "ул. Кабинет 1, 8")
    elsewhere = _place(salon, staff, "ул. Кабинет 2, 9")
    ss = _offering(salon, category, staff, "Пилинг два мастера", LC.MEDICAL_COSMETOLOGY)
    _license(salon, staff, covers=[ss.template], places=[licensed])
    here, there = _master("m12", licensed), _master("m13", elsewhere)

    result = address_states([(here.pk, ss.pk), (there.pk, ss.pk)])

    assert result == {(here.pk, ss.pk): VERIFIED, (there.pk, ss.pk): ADDRESS_MISMATCH}


def test_the_pool_is_read_in_three_queries(salon, category, staff, django_assert_num_queries) -> None:
    place = _place(salon, staff, "ул. Пул, 10")
    peel = _offering(salon, category, staff, "Пул пилинг", LC.MEDICAL_COSMETOLOGY)
    wrap = _offering(salon, category, staff, "Пул обёртывание", LC.NON_MEDICAL_COSMETIC)
    _license(salon, staff, covers=[peel.template], places=[place])
    m = _master("m14", place)
    pairs = [(m.pk, peel.pk), (m.pk, wrap.pk)]

    with django_assert_num_queries(3):
        result = address_states(pairs)

    assert [result[p] for p in pairs] == [VERIFIED, NOT_REQUIRED]


def test_unknown_ids_are_left_out(salon, category, staff) -> None:
    ss = _offering(salon, category, staff, "Одно", LC.NON_MEDICAL_COSMETIC)
    m = _master("m15")

    assert set(address_states([(m.pk, ss.pk), (m.pk, uuid.uuid4()), (uuid.uuid4(), ss.pk)])) == {
        (m.pk, ss.pk)
    }
