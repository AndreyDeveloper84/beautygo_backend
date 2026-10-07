"""DRF-2866, фаза 1 — область по разделам справочника и класс шести канонам.

Решение владельца 07.10: классификация правдива, допуск считается отдельно.
Миграция ``0049`` схему не меняет — это данные; узлы заводят каноны живыми
моделями и зовут шаг напрямую.

Узлы держат:

* область ``not_body_care`` получают каноны бесспорных разделов — включая
  рискованные (лазер, инъекции, пилинги лица), и их это не открывает;
* ``1.4``, раздел ``2``, раздел ``3``, ``17``–``20`` и каноны без кода
  остаются с неизвестной областью;
* класс ``non_medical_cosmetic`` получают ровно шесть канонов, автором —
  именная учётка владельца; им же она ставит область;
* под включённым флагом открыты только шесть; остальные закрыты каждая
  своей причиной — «класс не подтверждён» или «область неизвестна»;
* уже стоящие область и класс шаг не переписывает;
* без учётки владельца шаг падает и ничего не пишет; базу без шестёрки
  оставляет в покое;
* обратный ход снимает только своё.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as live_apps
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from services.body_care_license import CLASS_UNCONFIRMED, NOT_REQUIRED, license_states
from services.body_care_validation import NOT_SUBJECT, UNCLASSIFIED, validation_states
from services.migrations import _drf2866_phase1_classification as phase1
from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.migrations import _owner_provenance_account as owner_account
from users.models import User

pytestmark = pytest.mark.django_db

MIGRATION = importlib.import_module("services.migrations.0049_drf2866_phase1_classification")
Scope = ServiceTemplate.BodyCareScope
LC = ServiceTemplate.LegalServiceClass

#: Рискованные и прочие бесспорно-вне-Body-Care каноны пилота.
OUTSIDE = ("1.3.24", "4.2.3", "4.5.1", "4.6.4", "7.1.1", "7.1.20", "6.1.1", "10.1.1", "16.0.1", "22.0.1")
#: Правилом раздела не решаются.
UNDECIDED = ("1.4.1", "1.4.18", "2.2.2", "2.4.9", "3.1.1", "3.2.1", "17.1.1", "18.0.1", "19.0.1", "20.2.2")

SCOPE_FIELDS = (
    "body_care_scope", "scope_confirmed_by_id", "scope_confirmed_rule",
    "scope_rule_version", "scope_confirmed_at", "scope_source_ref",
)
CLASS_FIELDS = (
    "legal_service_class", "legal_class_confirmed_by_id",
    "legal_class_confirmed_at", "legal_class_source_ref",
)


@pytest.fixture
def owner(db):
    pk, _ = owner_account.ensure(User, unusable_password=make_password(None))
    return User.objects.get(pk=pk)


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Фаза 1", slug="phase1-2866")


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="phase1-salon", name="Салон фазы 1")


def _canon(category, code) -> ServiceTemplate:
    name = f"Канон {code or 'без кода'}"
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], canonical_code=code
    )


@pytest.fixture
def canons(category):
    codes = (*phase1.NON_MEDICAL_CODES, *OUTSIDE, *UNDECIDED, None)
    return {code: _canon(category, code) for code in codes}


def _classify(owner):
    return phase1.classify(ServiceTemplate, owner_id=owner.pk if owner else None, now=timezone.now())


def _row(canon, fields):
    return ServiceTemplate.objects.filter(pk=canon.pk).values(*fields).get()


def _scopes(canons, codes):
    return {
        code: scope
        for code, scope in ServiceTemplate.objects.filter(
            pk__in=[canons[c].pk for c in codes]
        ).values_list("canonical_code", "body_care_scope")
    }


# ─── область по разделам ─────────────────────────────────────────────────────


def test_the_undisputed_sections_get_not_body_care_by_the_rule(canons, owner) -> None:
    counts = _classify(owner)

    assert _scopes(canons, OUTSIDE) == dict.fromkeys(OUTSIDE, Scope.NOT_BODY_CARE)
    assert counts["scoped_by_rule"] == len(OUTSIDE)
    laser = _row(canons["7.1.1"], SCOPE_FIELDS)
    assert (laser["scope_confirmed_by_id"], laser["scope_confirmed_rule"], laser["scope_rule_version"]) == (
        None, phase1.RULE, phase1.RULE_VERSION,
    )
    assert laser["scope_confirmed_at"] is not None
    assert laser["scope_source_ref"] == phase1.SCOPE_RULE_SOURCE_REF


def test_the_disputed_sections_and_codeless_canons_stay_unknown(canons, owner) -> None:
    counts = _classify(owner)

    assert _scopes(canons, UNDECIDED) == dict.fromkeys(UNDECIDED, None)
    assert _row(canons[None], SCOPE_FIELDS)["body_care_scope"] is None
    assert counts["left_unknown"] == len(UNDECIDED) + 1


@pytest.mark.parametrize(
    ("code", "outside"),
    [
        ("1.1.1", True), ("1.3.24", True), ("1.4.1", False), ("1.6.2", True),
        ("2.1.1", False), ("3.3.3", False), ("4.6.4", True), ("7.1.20", True),
        ("16.0.1", True), ("17.1.1", False), ("19.0.5", False), ("20.2.2", False),
        ("21.0.1", True), ("22.0.9", True), ("23.1.1", False), (None, False), ("", False),
    ],
)
def test_the_section_rule(code, outside) -> None:
    assert phase1.section_is_outside_body_care(code) is outside


# ─── класс — шести, человеком ────────────────────────────────────────────────


def test_exactly_the_six_get_the_non_medical_class_from_the_owner(canons, owner) -> None:
    counts = _classify(owner)

    classed = set(
        ServiceTemplate.objects.filter(legal_service_class__isnull=False).values_list(
            "canonical_code", flat=True
        )
    )
    assert classed == set(phase1.NON_MEDICAL_CODES)
    assert counts["classed"] == counts["scoped_by_owner"] == 6
    massage = _row(canons["1.1.1"], (*CLASS_FIELDS, *SCOPE_FIELDS))
    assert massage["legal_service_class"] == LC.NON_MEDICAL_COSMETIC
    assert massage["legal_class_confirmed_by_id"] == owner.pk
    assert massage["legal_class_source_ref"] == phase1.OWNER_SOURCE_REF
    assert massage["legal_class_confirmed_at"] is not None
    # Область шестерым ставит тот же автор, не правило.
    assert (massage["body_care_scope"], massage["scope_confirmed_by_id"], massage["scope_confirmed_rule"]) == (
        Scope.NOT_BODY_CARE, owner.pk, "",
    )
    assert massage["scope_source_ref"] == phase1.OWNER_SOURCE_REF


def test_the_practitioner_class_and_the_health_flag_are_left_alone(canons, owner) -> None:
    _classify(owner)

    assert not ServiceTemplate.objects.filter(required_practitioner_class__isnull=False).exists()
    assert not ServiceTemplate.objects.filter(requires_health_check=True).exists()
    assert not ServiceTemplate.objects.filter(service_family__isnull=False).exists()


# ─── допуск после шага, под включённым флагом ────────────────────────────────


def test_only_the_six_open_and_everything_else_closes_for_its_own_reason(
    settings, canons, owner, category, salon
) -> None:
    settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED = True
    offers = {
        code: SalonService.objects.create(
            tenant=salon, category=category, template=canon, name=canon.name,
            duration_minutes=60, base_price=Decimal("3000"),
        )
        for code, canon in canons.items()
    }
    no_canon = SalonService.objects.create(
        tenant=salon, category=category, template=None, name="Без канона",
        duration_minutes=60, base_price=Decimal("3000"),
    )
    _classify(owner)

    pks = [o.pk for o in offers.values()] + [no_canon.pk]
    config, licence = validation_states(pks), license_states(pks)
    verdict = {code: (config[o.pk], licence[o.pk]) for code, o in offers.items()}

    # Шесть: проверка Body Care не применяется, юридические проверки сняты.
    assert {verdict[c] for c in phase1.NON_MEDICAL_CODES} == {(NOT_SUBJECT, NOT_REQUIRED)}
    # Бесспорно вне Body Care, но класса нет: закрывает юридическая проверка.
    assert {verdict[c] for c in OUTSIDE} == {(NOT_SUBJECT, CLASS_UNCONFIRMED)}
    # Область неизвестна: закрывают обе проверки, каждая своей причиной.
    assert {verdict[c] for c in (*UNDECIDED, None)} == {(UNCLASSIFIED, CLASS_UNCONFIRMED)}
    assert (config[no_canon.pk], licence[no_canon.pk]) == (UNCLASSIFIED, CLASS_UNCONFIRMED)


def test_with_the_flag_off_the_step_changes_no_answer(canons, owner, category, salon) -> None:
    offers = [
        SalonService.objects.create(
            tenant=salon, category=category, template=canon, name=canon.name,
            duration_minutes=60, base_price=Decimal("3000"),
        )
        for canon in canons.values()
    ]
    pks = [o.pk for o in offers]
    before = (validation_states(pks), license_states(pks))

    _classify(owner)

    assert (validation_states(pks), license_states(pks)) == before


# ─── чего шаг не переписывает ────────────────────────────────────────────────


def test_a_scope_and_a_class_already_set_are_not_rewritten(canons, owner) -> None:
    lawyer = User.objects.create_user(username="phase1-lawyer", password="x")
    decided, scoped = canons["1.1.1"], canons["7.1.1"]
    ServiceTemplate.objects.filter(pk=decided.pk).update(
        legal_service_class=LC.MEDICAL_OTHER, legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
    )
    ServiceTemplate.objects.filter(pk=scoped.pk).update(
        body_care_scope=Scope.BODY_CARE, scope_confirmed_by=lawyer,
        scope_confirmed_at=timezone.now(), scope_source_ref="решение куратора",
    )
    before = (_row(decided, CLASS_FIELDS), _row(scoped, SCOPE_FIELDS))

    counts = _classify(owner)

    assert (_row(decided, CLASS_FIELDS), _row(scoped, SCOPE_FIELDS)) == before
    assert counts["classed"] == 5


def test_a_second_run_changes_nothing(canons, owner) -> None:
    _classify(owner)
    before = list(ServiceTemplate.objects.order_by("pk").values(*SCOPE_FIELDS, *CLASS_FIELDS))

    counts = _classify(owner)

    assert list(ServiceTemplate.objects.order_by("pk").values(*SCOPE_FIELDS, *CLASS_FIELDS)) == before
    assert (counts["classed"], counts["scoped_by_owner"], counts["scoped_by_rule"]) == (0, 0, 0)


# ─── когда шаг падает ────────────────────────────────────────────────────────


def test_without_the_owner_account_the_step_refuses_and_writes_nothing(canons) -> None:
    with pytest.raises(phase1.CannotAttribute, match="учётки владельца нет"):
        _classify(None)

    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()
    assert not ServiceTemplate.objects.filter(body_care_scope__isnull=False).exists()


def test_a_database_without_the_six_needs_no_owner(category) -> None:
    laser = _canon(category, "7.1.1")

    counts = _classify(None)

    assert counts["classed"] == 0
    assert _row(laser, SCOPE_FIELDS)["body_care_scope"] == Scope.NOT_BODY_CARE


# ─── обратный ход ────────────────────────────────────────────────────────────


def test_the_reverse_restores_the_state_before(canons, owner) -> None:
    before = list(ServiceTemplate.objects.order_by("pk").values(*SCOPE_FIELDS, *CLASS_FIELDS))
    _classify(owner)

    counts = phase1.declassify(ServiceTemplate)

    assert list(ServiceTemplate.objects.order_by("pk").values(*SCOPE_FIELDS, *CLASS_FIELDS)) == before
    assert counts == {"declassed": 6, "unscoped": 6 + len(OUTSIDE)}


def test_the_reverse_leaves_later_human_decisions(canons, owner) -> None:
    _classify(owner)
    reclassed, rescoped = canons["1.1.4"], canons["7.1.20"]
    ServiceTemplate.objects.filter(pk=reclassed.pk).update(legal_service_class=LC.MEDICAL_OTHER)
    ServiceTemplate.objects.filter(pk=rescoped.pk).update(scope_source_ref="куратор: подтверждаю")
    kept = (_row(reclassed, CLASS_FIELDS), _row(rescoped, SCOPE_FIELDS))

    phase1.declassify(ServiceTemplate)

    assert (_row(reclassed, CLASS_FIELDS), _row(rescoped, SCOPE_FIELDS)) == kept


# ─── миграция и её литералы ──────────────────────────────────────────────────


def test_the_migration_names_the_owner_account_and_reverses(canons, owner) -> None:
    MIGRATION.classify(live_apps, None)
    assert ServiceTemplate.objects.filter(
        legal_service_class=LC.NON_MEDICAL_COSMETIC, legal_class_confirmed_by=owner
    ).count() == 6

    MIGRATION.declassify(live_apps, None)
    assert not ServiceTemplate.objects.filter(legal_service_class__isnull=False).exists()
    assert not ServiceTemplate.objects.filter(body_care_scope__isnull=False).exists()


def test_the_migration_refuses_when_the_owner_account_is_switched_off(canons, owner) -> None:
    User.objects.filter(pk=owner.pk).update(is_active=False)

    with pytest.raises(phase1.CannotAttribute):
        MIGRATION.classify(live_apps, None)


def test_the_literals_match_the_model() -> None:
    assert phase1.NOT_BODY_CARE == Scope.NOT_BODY_CARE.value
    assert phase1.NON_MEDICAL_COSMETIC == LC.NON_MEDICAL_COSMETIC.value
    field = ServiceTemplate._meta.get_field
    assert 0 < len(phase1.SCOPE_RULE_SOURCE_REF) <= field("scope_source_ref").max_length
    assert 0 < len(phase1.OWNER_SOURCE_REF) <= field("legal_class_source_ref").max_length
    assert len(phase1.RULE) <= field("scope_confirmed_rule").max_length
    assert len(set(phase1.NON_MEDICAL_CODES)) == 6
    # Шестёрка сама лежит в бесспорных разделах: правило и решение владельца
    # о ней не расходятся.
    assert all(phase1.section_is_outside_body_care(code) for code in phase1.NON_MEDICAL_CODES)


def test_the_rule_agrees_with_the_reference_catalogue() -> None:
    """Каждый раздел эталонного справочника правилом либо решён, либо назван нерешаемым."""
    import json
    from pathlib import Path

    rows = json.loads(
        (Path(phase1.__file__).resolve().parents[1] / "seeds" / "canonical_catalog_2026-07.json")
        .read_text(encoding="utf-8")
    )
    outside = {r["code"].rsplit(".", 1)[0] for r in rows if phase1.section_is_outside_body_care(r["code"])}
    undecided = {r["code"].rsplit(".", 1)[0] for r in rows} - outside

    assert undecided == {
        "1.4", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3",
        "17.1", "18.0", "19.0", "20.1", "20.2", "20.3",
    }
    assert sum(phase1.section_is_outside_body_care(r["code"]) for r in rows) == 943
