"""Body Care §7A-4: квалификация мастера и выводимое состояние пары (DRF-2840).

Shape одобрен главным окном 06.10: квалификация — факт мастера, проверка —
провенанс человека («все три или ни одного»), ``protocol_ref`` только у
``protocol_specific``; состояние — по паре (мастер, канон), читает только
подтверждённые поля §7A-0; точное совпадение класса (Q1); подтверждённое
требование проверяется независимо от класса (Q2); ``protocol_specific`` —
всегда ``not_verified`` (Q3); при стирании аккаунта строки удаляются (Q4).
"""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.body_care_qualification import (
    CODES,
    NOT_REQUIRED,
    NOT_VERIFIED,
    PROTOCOL_REASON,
    QUALIFICATION_STATES,
    REQUIREMENT_UNCONFIRMED,
    VERIFIED,
    qualification_state,
    qualification_states,
)
from services.models import ServiceCategory, ServiceTemplate
from users.models import DeletionRequest, PractitionerQualification, SpecialistProfile, User

pytestmark = pytest.mark.django_db

LC = ServiceTemplate.LegalServiceClass
PC = ServiceTemplate.PractitionerClass


@pytest.fixture
def reviewer(db):
    return User.objects.create_user(username="bc7a4-reviewer", password="x")


@pytest.fixture
def master(db):
    user = User.objects.create_user(username="bc7a4-master", password="x")
    return SpecialistProfile.objects.create(user=user, display_name="Мастер 7A-4")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-4", slug="bc-7a4")


def _canon(category, reviewer, name, legal_class=None, required=None) -> ServiceTemplate:
    tpl = ServiceTemplate.objects.create(
        category=category,
        name=name,
        name_short=name[:40],
        service_family=ServiceTemplate.ServiceFamily.BODY_WRAP,
        canonical_version="1",
    )
    fields = {}
    if legal_class is not None:
        fields.update(
            legal_service_class=legal_class,
            legal_class_confirmed_by=reviewer,
            legal_class_confirmed_at=timezone.now(),
            legal_class_source_ref="решение юриста",
        )
    if required is not None:
        fields.update(
            required_practitioner_class=required,
            practitioner_class_confirmed_by=reviewer,
            practitioner_class_confirmed_at=timezone.now(),
            practitioner_class_source_ref="решение клиники",
        )
    if fields:
        ServiceTemplate.objects.filter(pk=tpl.pk).update(**fields)
    return tpl


def _qualify(master, cls, reviewer=None, protocol_ref="") -> PractitionerQualification:
    q = PractitionerQualification.objects.create(
        specialist=master, practitioner_class=cls, protocol_ref=protocol_ref
    )
    if reviewer is not None:
        PractitionerQualification.objects.filter(pk=q.pk).update(
            verified_by=reviewer, verified_at=timezone.now(), verification_source_ref="диплом сверен"
        )
    return q


# ─── модель ──────────────────────────────────────────────────────────────────


def test_the_vocabulary_is_the_same_as_on_the_canon() -> None:
    assert set(PractitionerQualification.PractitionerClass.values) == set(PC.values)


def test_a_class_outside_the_vocabulary_is_refused(master) -> None:
    with pytest.raises(IntegrityError, match="practitionerqualification_class_known"):
        with transaction.atomic():
            PractitionerQualification.objects.create(specialist=master, practitioner_class="any")


def test_protocol_specific_requires_a_protocol_ref(master) -> None:
    with pytest.raises(IntegrityError, match="protocol_ref_iff_protocol_specific"):
        with transaction.atomic():
            PractitionerQualification.objects.create(
                specialist=master, practitioner_class=PC.PROTOCOL_SPECIFIC
            )


def test_another_class_must_not_carry_a_protocol_ref(master) -> None:
    with pytest.raises(IntegrityError, match="protocol_ref_iff_protocol_specific"):
        with transaction.atomic():
            PractitionerQualification.objects.create(
                specialist=master, practitioner_class=PC.NURSE_COSMETOLOGY, protocol_ref="P-1"
            )


@pytest.mark.parametrize("present", ["by", "at", "source_ref", "by+at", "at+source_ref"])
def test_a_partial_verification_is_refused(master, reviewer, present) -> None:
    q = _qualify(master, PC.NURSE_COSMETOLOGY)
    fields = {}
    if "by" in present:
        fields["verified_by"] = reviewer
    if "at" in present:
        fields["verified_at"] = timezone.now()
    if "source_ref" in present:
        fields["verification_source_ref"] = "диплом"

    with pytest.raises(IntegrityError, match="practitionerqualification_verification_all_or_nothing"):
        with transaction.atomic():
            PractitionerQualification.objects.filter(pk=q.pk).update(**fields)


def test_the_same_qualification_is_recorded_once(master) -> None:
    _qualify(master, PC.NURSE_COSMETOLOGY)

    with pytest.raises(IntegrityError, match="practitionerqualification_unique"):
        with transaction.atomic():
            _qualify(master, PC.NURSE_COSMETOLOGY)


# ─── стирание аккаунта (Q4) и перепись ───────────────────────────────────────


def test_erasure_deletes_the_masters_qualifications(master, reviewer) -> None:
    from users.deletion_executor import _erase_qualifications

    _qualify(master, PC.NURSE_COSMETOLOGY, reviewer)
    _qualify(master, PC.PROTOCOL_SPECIFIC, protocol_ref="P-1")
    anonymised: dict = {}

    _erase_qualifications(master, anonymised)

    assert not PractitionerQualification.objects.filter(specialist=master).exists()
    assert anonymised["users.PractitionerQualification.deleted"] == 2


def test_erasure_leaves_other_masters_alone(master, reviewer) -> None:
    from users.deletion_executor import _erase_qualifications

    other_user = User.objects.create_user(username="bc7a4-other", password="x")
    other = SpecialistProfile.objects.create(user=other_user, display_name="Другой мастер")
    _qualify(other, PC.NURSE_COSMETOLOGY, reviewer)

    _erase_qualifications(master, {})

    assert PractitionerQualification.objects.filter(specialist=other).count() == 1


def test_account_erasure_end_to_end_removes_the_qualifications(master, reviewer) -> None:
    """Не помощник напрямую, а исполнитель заявки: шаг стирания вызывается."""
    from users.deletion_executor import BotConfirmation, execute
    from users.deletion_requests import ensure_deletion_request

    class _BotOk:
        def confirm(self, **kw):
            return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})

    _qualify(master, PC.NURSE_COSMETOLOGY, reviewer)
    req = ensure_deletion_request(master.user, initiator="bot").request

    out = execute(req, bot_client=_BotOk())

    assert out.status == DeletionRequest.Status.COMPLETED, out
    assert not PractitionerQualification.objects.filter(specialist=master).exists()


def test_a_left_qualification_is_reported_as_residue(master) -> None:
    from users.deletion_executor import _own_place_residue

    _qualify(master, PC.NURSE_COSMETOLOGY)

    assert _own_place_residue(master).get("users.PractitionerQualification") == 1


def test_the_erasure_census_decides_the_reviewer() -> None:
    from users.deletion_executor import RETAIN

    assert "users.PractitionerQualification.verified_by" in RETAIN


# ─── выводимое состояние ────────────────────────────────────────────────────


def test_the_states_and_codes() -> None:
    assert set(QUALIFICATION_STATES) == {
        "not_required", "requirement_unconfirmed", "not_verified", "verified",
    }
    assert CODES == {"not_verified": "PRACTITIONER_QUALIFICATION_NOT_VERIFIED"}


def test_a_verified_exact_qualification_is_verified(master, reviewer, category) -> None:
    peel = _canon(category, reviewer, "Пилинг", LC.MEDICAL_COSMETOLOGY, PC.NURSE_COSMETOLOGY)
    _qualify(master, PC.NURSE_COSMETOLOGY, reviewer)

    assert qualification_state(master.pk, peel.pk).state == VERIFIED


def test_an_unverified_qualification_does_not_count(master, reviewer, category) -> None:
    peel = _canon(category, reviewer, "Пилинг без проверки", LC.MEDICAL_COSMETOLOGY, PC.NURSE_COSMETOLOGY)
    _qualify(master, PC.NURSE_COSMETOLOGY)

    assert qualification_state(master.pk, peel.pk).state == NOT_VERIFIED


def test_a_higher_class_does_not_cover_a_lower_one(master, reviewer, category) -> None:
    """Q1: точное совпадение; иерархия — решение клиники (D-2)."""
    peel = _canon(category, reviewer, "Пилинг врач", LC.MEDICAL_COSMETOLOGY, PC.NURSE_COSMETOLOGY)
    _qualify(master, PC.PHYSICIAN_COSMETOLOGIST, reviewer)

    assert qualification_state(master.pk, peel.pk).state == NOT_VERIFIED


def test_another_masters_qualification_does_not_count(master, reviewer, category) -> None:
    peel = _canon(category, reviewer, "Пилинг чужой", LC.MEDICAL_COSMETOLOGY, PC.NURSE_COSMETOLOGY)
    other_user = User.objects.create_user(username="bc7a4-other2", password="x")
    other = SpecialistProfile.objects.create(user=other_user, display_name="Другой")
    _qualify(other, PC.NURSE_COSMETOLOGY, reviewer)

    assert qualification_state(master.pk, peel.pk).state == NOT_VERIFIED


def test_a_confirmed_requirement_is_checked_for_a_non_medical_class(master, reviewer, category) -> None:
    """Q2a: клиника сказала «нужна квалификация» — исполняем и для немедицинского."""
    wrap = _canon(category, reviewer, "Обёртывание", LC.NON_MEDICAL_COSMETIC, PC.COSMETIC_ESTHETICIAN)

    assert qualification_state(master.pk, wrap.pk).state == NOT_VERIFIED


@pytest.mark.parametrize("legal_class", [LC.MEDICAL_COSMETOLOGY, LC.MEDICAL_OTHER])
def test_a_medical_class_without_a_requirement_is_unconfirmed(master, reviewer, category, legal_class) -> None:
    peel = _canon(category, reviewer, f"Медицинская {legal_class}", legal_class)

    assert qualification_state(master.pk, peel.pk).state == REQUIREMENT_UNCONFIRMED


def test_a_non_medical_class_without_a_requirement_needs_nothing(master, reviewer, category) -> None:
    wrap = _canon(category, reviewer, "Обёртывание без требования", LC.NON_MEDICAL_COSMETIC)

    assert qualification_state(master.pk, wrap.pk).state == NOT_REQUIRED


def test_protocol_specific_is_never_verified_yet(master, reviewer, category) -> None:
    """Q3: у канона нет ссылки на протокол — сверять не с чем (D-2)."""
    proc = _canon(category, reviewer, "По протоколу", LC.MEDICAL_COSMETOLOGY, PC.PROTOCOL_SPECIFIC)
    _qualify(master, PC.PROTOCOL_SPECIFIC, reviewer, protocol_ref="P-1")

    result = qualification_state(master.pk, proc.pk)

    assert result.state == NOT_VERIFIED
    assert result.reason == PROTOCOL_REASON


def test_the_pool_is_read_in_two_queries(master, reviewer, category, django_assert_num_queries) -> None:
    peel = _canon(category, reviewer, "Пул пилинг", LC.MEDICAL_COSMETOLOGY, PC.NURSE_COSMETOLOGY)
    wrap = _canon(category, reviewer, "Пул обёртывание", LC.NON_MEDICAL_COSMETIC)
    bare = _canon(category, reviewer, "Пул медицинский", LC.MEDICAL_OTHER)
    _qualify(master, PC.NURSE_COSMETOLOGY, reviewer)
    pairs = [(master.pk, t.pk) for t in (peel, wrap, bare)]

    with django_assert_num_queries(2):
        result = qualification_states(pairs)

    assert [result[p].state for p in pairs] == [VERIFIED, NOT_REQUIRED, REQUIREMENT_UNCONFIRMED]


def test_an_unknown_canon_is_left_out(master, reviewer, category) -> None:
    wrap = _canon(category, reviewer, "Одно")

    assert set(qualification_states([(master.pk, wrap.pk), (master.pk, uuid.uuid4())])) == {
        (master.pk, wrap.pk)
    }
