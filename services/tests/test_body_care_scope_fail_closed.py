"""Область классификации канона и правило «неизвестное не допускается».

Решение владельца 07.10: отсутствие семейства перестаёт значить «вне Body
Care». Классификация правдива, допуск считается отдельно: ``not_body_care``
пропускает только проверку Body Care, а юридический класс проверяется у
любого канона.

Узлы держат:

* ``scope_of``: неизвестная область, ``body_care`` без семейства и
  предложение без канона — ``unclassified``; ``not_subject`` рождается
  только из подтверждённого ``not_body_care``;
* шесть читателей (CAT-6, требования, кандидат класса, лицензия, адрес,
  квалификация) отвечают по одному правилу;
* неподтверждённый класс не снимает юридические проверки ни у какого
  канона; снимает только подтверждённый немедицинский;
* при выключенном флаге поведение прежнее — обход открыт;
* база отклоняет противоречивые сочетания области и семейства и область
  без провенанса («кто ИЛИ правило», не оба и не ни одного);
* назначенное семейство ставит область правилом, а область человека
  правило не переписывает;
* смена версии канона делает ревью конфигурации неактуальным; отпечаток
  зависит от области, семейства и версии;
* указатель на подтвердившего область решён переписью удаления.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services import body_care_scope
from services.body_care_address import address_states
from services.body_care_legal import legal_class_candidates
from services.body_care_license import CLASS_UNCONFIRMED, NOT_REQUIRED, NOT_VERIFIED, license_states
from services.body_care_qualification import (
    NOT_REQUIRED as QUALIFICATION_NOT_REQUIRED,
    REQUIREMENT_UNCONFIRMED,
    qualification_states,
)
from services.body_care_requirements import check_configurations
from services.body_care_scope import NOT_SUBJECT, SUBJECT, UNCLASSIFIED, classification_stamp, scope_of
from services.body_care_validation import (
    INCOMPLETE,
    READY_FOR_SCREENING,
    REVIEW_REQUIRED,
    config_fingerprint,
    record_config_review,
    validation_states,
)
from services.models import OfferingConfigFact, SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.deletion_executor import RETAIN
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("services.migrations.0048_body_care_scope")
Scope = ServiceTemplate.BodyCareScope
Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass
F = OfferingConfigFact.Field

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
def closed(settings):
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="scope-staff", password="x")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="scope-salon", name="Салон области")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Область 2852", slug="scope-cat")


@pytest.fixture
def master(db):
    user = User.objects.create_user(username="scope-master", password="x")
    return SpecialistProfile.objects.create(user=user, display_name="Мастер области")


def _canon(category, name, *, family=None, scope=None, legal_class=None, by=None) -> ServiceTemplate:
    canon = ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40],
        service_family=family, canonical_version="1" if family else "",
    )
    if scope is not None:
        _set_scope(canon, scope, by)
    if legal_class is not None:
        ServiceTemplate.objects.filter(pk=canon.pk).update(
            legal_service_class=legal_class, legal_class_confirmed_by=by,
            legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
        )
    return canon


def _set_scope(canon, scope, by) -> None:
    ServiceTemplate.objects.filter(pk=canon.pk).update(
        body_care_scope=scope, scope_confirmed_by=by, scope_confirmed_rule="",
        scope_rule_version="", scope_confirmed_at=timezone.now(), scope_source_ref="решение владельца",
    )


def _offer(salon, category, canon, name="Предложение") -> SalonService:
    return SalonService.objects.create(
        tenant=salon, category=category, template=canon, name=name,
        duration_minutes=60, base_price=Decimal("3000"), configuration_version="cfg-1",
    )


def _refused(canon, constraint, **fields) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        with transaction.atomic():
            ServiceTemplate.objects.filter(pk=canon.pk).update(**fields)


PROVENANCE = {"scope_confirmed_at": timezone.now(), "scope_source_ref": "решение владельца"}


# ─── правило ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("has_canon", "scope", "family", "answer"),
    [
        (True, None, None, UNCLASSIFIED),
        (False, None, None, UNCLASSIFIED),
        (True, Scope.NOT_BODY_CARE, None, NOT_SUBJECT),
        (True, Scope.BODY_CARE, Family.BODY_WRAP, SUBJECT),
        (True, Scope.BODY_CARE, None, UNCLASSIFIED),
        (True, "нечто", None, UNCLASSIFIED),
        # Противоречие база не пропустит; правило его тоже не открывает.
        (True, None, Family.BODY_WRAP, UNCLASSIFIED),
    ],
)
def test_only_a_confirmed_not_body_care_is_not_subject(closed, has_canon, scope, family, answer) -> None:
    assert scope_of(has_canon=has_canon, scope=scope, family=family) == answer


@pytest.mark.parametrize(
    ("has_canon", "scope", "family", "answer"),
    [
        (True, None, None, NOT_SUBJECT),
        (False, None, None, NOT_SUBJECT),
        (True, None, Family.BODY_WRAP, SUBJECT),
        (True, Scope.NOT_BODY_CARE, None, NOT_SUBJECT),
    ],
)
def test_with_the_flag_off_the_bypass_is_still_open(has_canon, scope, family, answer) -> None:
    assert body_care_scope.fail_closed() is False
    assert scope_of(has_canon=has_canon, scope=scope, family=family) == answer


@pytest.mark.parametrize(
    ("legal_class", "waived"),
    [
        (LC.NON_MEDICAL_COSMETIC, True),
        (None, False),
        (LC.LEGAL_REVIEW_REQUIRED, False),
        (LC.MEDICAL_COSMETOLOGY, False),
        (LC.MEDICAL_OTHER, False),
    ],
)
def test_only_a_confirmed_non_medical_class_waives_the_legal_checks(closed, legal_class, waived) -> None:
    for family in (None, Family.BODY_WRAP):
        assert body_care_scope.legal_checks_waived(legal_class=legal_class, family=family) is waived


# ─── CAT-6 ───────────────────────────────────────────────────────────────────


def test_cat6_names_the_unclassified_and_skips_only_the_confirmed(closed, salon, category, staff) -> None:
    unknown = _offer(salon, category, _canon(category, "Неизвестная"))
    outside = _offer(salon, category, _canon(category, "Массаж", scope=Scope.NOT_BODY_CARE, by=staff))
    no_family = _offer(salon, category, _canon(category, "Лимфодренаж", scope=Scope.BODY_CARE, by=staff))
    no_canon = _offer(salon, category, None)
    wrap = _offer(salon, category, _canon(category, "Обёртывание", family=Family.BODY_WRAP))

    states = validation_states([o.pk for o in (unknown, outside, no_family, no_canon, wrap)])

    assert states == {
        unknown.pk: UNCLASSIFIED,
        outside.pk: NOT_SUBJECT,
        no_family.pk: UNCLASSIFIED,
        no_canon.pk: UNCLASSIFIED,
        wrap.pk: INCOMPLETE,
    }


def test_cat6_with_the_flag_off_reads_no_family_as_not_subject(salon, category) -> None:
    unknown = _offer(salon, category, _canon(category, "Неизвестная"))
    no_canon = _offer(salon, category, None)

    assert validation_states([unknown.pk, no_canon.pk]) == {
        unknown.pk: NOT_SUBJECT, no_canon.pk: NOT_SUBJECT,
    }


def test_the_requirements_and_the_class_candidate_follow_the_same_rule(closed, salon, category, staff) -> None:
    unknown = _canon(category, "Неизвестная")
    outside = _canon(category, "Массаж", scope=Scope.NOT_BODY_CARE, by=staff)
    offers = {c.pk: _offer(salon, category, c) for c in (unknown, outside)}

    checks = check_configurations([o.pk for o in offers.values()])
    candidates = legal_class_candidates([unknown.pk, outside.pk])

    assert checks[offers[unknown.pk].pk].state == UNCLASSIFIED
    assert checks[offers[outside.pk].pk].state == NOT_SUBJECT
    assert (candidates[unknown.pk].value, candidates[unknown.pk].subject) == (None, True)
    assert candidates[unknown.pk].reason.startswith(UNCLASSIFIED)
    assert candidates[outside.pk].subject is False


# ─── юридический класс проверяется у любого канона ───────────────────────────


def test_an_unknown_class_closes_the_licence_gate_whatever_the_scope(closed, salon, category, staff) -> None:
    """``not_body_care`` — классификация, а не «немедицинская»."""
    outside = _offer(salon, category, _canon(category, "Лазер", scope=Scope.NOT_BODY_CARE, by=staff))
    review = _offer(salon, category, _canon(
        category, "Пилинг", scope=Scope.NOT_BODY_CARE, legal_class=LC.LEGAL_REVIEW_REQUIRED, by=staff,
    ))
    cleared = _offer(salon, category, _canon(
        category, "Массаж", scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC, by=staff,
    ))
    medical = _offer(salon, category, _canon(
        category, "Инъекция", scope=Scope.NOT_BODY_CARE, legal_class=LC.MEDICAL_COSMETOLOGY, by=staff,
    ))
    unknown = _offer(salon, category, _canon(category, "Неизвестная"))
    no_canon = _offer(salon, category, None)

    states = license_states([o.pk for o in (outside, review, cleared, medical, unknown, no_canon)])

    assert states == {
        outside.pk: CLASS_UNCONFIRMED,
        review.pk: CLASS_UNCONFIRMED,
        cleared.pk: NOT_REQUIRED,
        medical.pk: NOT_VERIFIED,
        unknown.pk: CLASS_UNCONFIRMED,
        no_canon.pk: CLASS_UNCONFIRMED,
    }


def test_an_unknown_class_closes_the_address_gate_too(closed, salon, category, staff, master) -> None:
    outside = _offer(salon, category, _canon(category, "Лазер", scope=Scope.NOT_BODY_CARE, by=staff))
    cleared = _offer(salon, category, _canon(
        category, "Массаж", scope=Scope.NOT_BODY_CARE, legal_class=LC.NON_MEDICAL_COSMETIC, by=staff,
    ))
    no_canon = _offer(salon, category, None)

    states = address_states([(master.pk, o.pk) for o in (outside, cleared, no_canon)])

    assert states == {
        (master.pk, outside.pk): CLASS_UNCONFIRMED,
        (master.pk, cleared.pk): NOT_REQUIRED,
        (master.pk, no_canon.pk): CLASS_UNCONFIRMED,
    }


def test_an_unknown_class_leaves_the_qualification_requirement_unknown(closed, category, staff, master) -> None:
    unknown = _canon(category, "Лазер", scope=Scope.NOT_BODY_CARE, by=staff)
    review = _canon(category, "Пилинг", legal_class=LC.LEGAL_REVIEW_REQUIRED, by=staff)
    cleared = _canon(category, "Массаж", legal_class=LC.NON_MEDICAL_COSMETIC, by=staff)

    states = qualification_states([(master.pk, c.pk) for c in (unknown, review, cleared)])

    assert states[(master.pk, unknown.pk)].state == REQUIREMENT_UNCONFIRMED
    assert states[(master.pk, review.pk)].state == REQUIREMENT_UNCONFIRMED
    assert states[(master.pk, cleared.pk)].state == QUALIFICATION_NOT_REQUIRED


def test_with_the_flag_off_the_legal_gates_keep_the_old_answers(salon, category, staff, master) -> None:
    unknown = _canon(category, "Неизвестная")
    offer = _offer(salon, category, unknown)

    assert license_states([offer.pk]) == {offer.pk: NOT_REQUIRED}
    assert address_states([(master.pk, offer.pk)]) == {(master.pk, offer.pk): NOT_REQUIRED}
    assert qualification_states([(master.pk, unknown.pk)])[(master.pk, unknown.pk)].state == (
        QUALIFICATION_NOT_REQUIRED
    )


# ─── база: сочетания и провенанс ─────────────────────────────────────────────


def test_a_scope_outside_the_vocabulary_is_refused(category, staff) -> None:
    canon = _canon(category, "Канон")

    _refused(
        canon, "servicetemplate_body_care_scope_known",
        body_care_scope="прочее", scope_confirmed_by=staff, **PROVENANCE,
    )


@pytest.mark.parametrize("scope", [None, Scope.NOT_BODY_CARE])
def test_a_family_without_the_body_care_scope_is_refused(category, staff, scope) -> None:
    wrap = _canon(category, "Обёртывание", family=Family.BODY_WRAP)
    by = staff if scope else None
    provenance = PROVENANCE if scope else {"scope_confirmed_at": None, "scope_source_ref": ""}

    _refused(
        wrap, "servicetemplate_family_requires_body_care_scope",
        body_care_scope=scope, scope_confirmed_by=by, scope_confirmed_rule="",
        scope_rule_version="", **provenance,
    )


def test_body_care_without_a_family_is_accepted(category, staff) -> None:
    """«Подлежит, семейство не определено» — честная запись, не противоречие."""
    canon = _canon(category, "Лимфодренаж", scope=Scope.BODY_CARE, by=staff)

    canon.refresh_from_db()
    assert (canon.body_care_scope, canon.service_family) == (Scope.BODY_CARE, None)


@pytest.mark.parametrize(
    "fields",
    [
        {"scope_confirmed_by": None, "scope_confirmed_rule": "", "scope_rule_version": ""},
        {"scope_confirmed_rule": "раздел", "scope_rule_version": "1"},  # и человек, и правило
        {"scope_confirmed_by": None, "scope_confirmed_rule": "раздел", "scope_rule_version": ""},
        {"scope_confirmed_at": None},
        {"scope_source_ref": ""},
    ],
    ids=["никто", "и-человек-и-правило", "правило-без-версии", "без-даты", "без-основания"],
)
def test_a_scope_without_honest_provenance_is_refused(category, staff, fields) -> None:
    canon = _canon(category, "Канон")
    full = {
        "body_care_scope": Scope.NOT_BODY_CARE, "scope_confirmed_by": staff,
        "scope_confirmed_rule": "", "scope_rule_version": "", **PROVENANCE,
    }

    _refused(canon, "servicetemplate_body_care_scope_requires_provenance", **{**full, **fields})


@pytest.mark.parametrize(
    "who",
    [
        {"scope_confirmed_rule": "", "scope_rule_version": ""},
        {"scope_confirmed_by": None, "scope_confirmed_rule": "раздел справочника", "scope_rule_version": "1"},
    ],
    ids=["человек", "правило"],
)
def test_a_scope_confirmed_by_a_person_or_by_a_rule_is_accepted(category, staff, who) -> None:
    canon = _canon(category, "Канон")
    fields = {"body_care_scope": Scope.NOT_BODY_CARE, "scope_confirmed_by": staff, **PROVENANCE, **who}

    ServiceTemplate.objects.filter(pk=canon.pk).update(**fields)

    canon.refresh_from_db()
    assert canon.body_care_scope == Scope.NOT_BODY_CARE


# ─── семейство ставит область правилом ───────────────────────────────────────


def test_a_family_sets_the_scope_by_the_rule(category) -> None:
    wrap = _canon(category, "Обёртывание", family=Family.BODY_WRAP)

    wrap.refresh_from_db()
    assert wrap.body_care_scope == Scope.BODY_CARE
    assert (wrap.scope_confirmed_by_id, wrap.scope_confirmed_rule, wrap.scope_rule_version) == (
        None, ServiceTemplate.SCOPE_BY_FAMILY_RULE, ServiceTemplate.SCOPE_BY_FAMILY_RULE_VERSION,
    )
    assert wrap.scope_confirmed_at is not None and wrap.scope_source_ref


def test_a_family_named_later_sets_the_scope_even_with_update_fields(category) -> None:
    canon = _canon(category, "Канон")
    canon.service_family = Family.BODY_WRAP
    canon.canonical_version = "1"

    canon.save(update_fields=["service_family", "canonical_version"])

    canon.refresh_from_db()
    assert canon.body_care_scope == Scope.BODY_CARE


def test_the_rule_does_not_rewrite_a_scope_set_by_a_person(category, staff) -> None:
    canon = _canon(category, "Лимфодренаж", scope=Scope.BODY_CARE, by=staff)
    canon.refresh_from_db()
    canon.service_family = Family.SPA_BODY
    canon.canonical_version = "1"

    canon.save()

    canon.refresh_from_db()
    assert (canon.scope_confirmed_by_id, canon.scope_confirmed_rule) == (staff.pk, "")


def test_a_canon_without_a_family_keeps_an_unknown_scope(category) -> None:
    canon = _canon(category, "Канон")

    canon.refresh_from_db()
    assert canon.body_care_scope is None


def test_the_migration_rule_matches_the_model_rule() -> None:
    assert MIGRATION.RULE == ServiceTemplate.SCOPE_BY_FAMILY_RULE
    assert MIGRATION.RULE_VERSION == ServiceTemplate.SCOPE_BY_FAMILY_RULE_VERSION
    assert 0 < len(MIGRATION.SOURCE_REF) <= ServiceTemplate._meta.get_field("scope_source_ref").max_length


# ─── ревью и классификация ───────────────────────────────────────────────────


def _ready(salon, category, staff) -> SalonService:
    wrap = _offer(salon, category, _canon(
        category, "Обёртывание", family=Family.BODY_WRAP, legal_class=LC.NON_MEDICAL_COSMETIC, by=staff,
    ))
    for field, value in WRAP.items():
        OfferingConfigFact.objects.create(
            salon_service=wrap, field=field, state=OfferingConfigFact.State.KNOWN, value=value,
            source_ref="инструкция", source_type="manufacturer_instruction",
        )
    record_config_review(wrap, by=staff, source_ref="ревью куратора")
    return wrap


@pytest.mark.parametrize("flag", [False, True], ids=["флаг-выключен", "флаг-включён"])
def test_a_new_canon_version_makes_the_review_stale(settings, salon, category, staff, flag) -> None:
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = flag
    wrap = _ready(salon, category, staff)
    assert validation_states([wrap.pk]) == {wrap.pk: READY_FOR_SCREENING}

    ServiceTemplate.objects.filter(pk=wrap.template_id).update(canonical_version="2")

    assert validation_states([wrap.pk]) == {wrap.pk: REVIEW_REQUIRED}


def test_the_fingerprint_depends_on_scope_family_and_version() -> None:
    base = {"scope": Scope.BODY_CARE, "family": Family.BODY_WRAP, "canonical_version": "1"}
    prints = {
        config_fingerprint([], classification_stamp(**{**base, **change}))
        for change in (
            {},
            {"scope": Scope.NOT_BODY_CARE},
            {"scope": None},
            {"family": Family.SPA_BODY},
            {"family": None},
            {"canonical_version": "2"},
        )
    }

    assert len(prints) == 6


# ─── перепись удаления ───────────────────────────────────────────────────────


def test_the_scope_confirmer_is_decided_in_the_deletion_census() -> None:
    assert "services.ServiceTemplate.scope_confirmed_by" in RETAIN
