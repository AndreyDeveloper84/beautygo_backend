"""Body Care CAT-5: требования конфигурации по семействам и INCOMPLETE (контракт §6).

Решения (главное окно, 06.10): до решения клиники обязательны ВСЕ «минимально
значимые» §6 (fail-closed, Р1); у скраба ``mechanical_mode`` = ``mode`` и
«длительность ИЛИ протокол» удовлетворяет любое из двух (Р2); SPA и кислоты
всегда ``incomplete``, пока их требования не выразимы фактами (Р3); словарь
требований — в коде с версией (Р4).

Узлы держат:

* услуга вне body-care — ``not_subject``, а не ``incomplete``: иначе её закрыл
  бы CAT-10 и подбор потерял бы весь не-body-care каталог;
* ничего не известно — ``incomplete`` со списком всех требований;
* все требования известны — ``complete``;
* ``NOT_APPLICABLE`` на требуемом поле не удовлетворяет (обход C3);
* требование из нескольких полей удовлетворяет любое известное поле;
* противоречие — ``conflict`` (BLOCKED для CAT-6), а не «не хватает»;
* SPA и кислоты — ``incomplete`` с причиной;
* пул считается двумя запросами, без N+1.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.body_care_requirements import (
    COMPLETE,
    CONFLICT,
    INCOMPLETE,
    NOT_SUBJECT,
    REQUIREMENTS,
    REQUIREMENTS_VERSION,
    check_configuration,
    check_configurations,
)
from services.models import OfferingConfigFact, SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State
Family = ServiceTemplate.ServiceFamily

WRAP_FIELDS = {
    F.PRODUCT_NAME: "Шоколадная маска",
    F.INSTRUCTION_VERSION: "v3",
    F.APPLICATION_AREA: "тело",
    F.COVERING_TYPE: "плёнка",
    F.EXPOSURE_SECONDS: 1800,
    F.REMOVAL_METHOD: "душ",
    F.ADDITIONAL_MODALITY: "нет",
}


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="cat5-salon", name="Салон CAT-5")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care CAT-5", slug="cat5-body")


def _offering(tenant, category, name, family=None, template=True) -> SalonService:
    tpl = None
    if template:
        tpl = ServiceTemplate.objects.create(
            category=category,
            name=f"Канон {name}",
            name_short=name[:40],
            service_family=family,
            canonical_version="1" if family else "",
        )
    return SalonService.objects.create(
        tenant=tenant,
        category=category,
        template=tpl,
        name=name,
        duration_minutes=60,
        base_price=Decimal("3000"),
    )


def _fact(offering, field, state=St.KNOWN, value="x") -> None:
    known = state == St.KNOWN
    OfferingConfigFact.objects.create(
        salon_service=offering,
        field=field,
        state=state,
        value=value if known or state == St.CONFLICT else None,
        source_ref="инструкция" if known else "",
        source_type="manufacturer_instruction" if known else None,
    )


def _complete_wrap(offering) -> None:
    for field, value in WRAP_FIELDS.items():
        _fact(offering, field, value=value)


# ─── словарь требований ──────────────────────────────────────────────────────


def test_the_requirements_carry_a_version() -> None:
    assert REQUIREMENTS_VERSION


def test_wrap_requires_every_minimally_significant_parameter_of_section_six() -> None:
    names = [r.name for r in REQUIREMENTS[Family.BODY_WRAP].requirements]
    assert names == [
        "product", "instruction", "area", "covering", "exposure", "removal",
        "additional_modality",
    ]


def test_scrub_reads_duration_or_protocol_as_either_field() -> None:
    reqs = {r.name: r.fields for r in REQUIREMENTS[Family.MECHANICAL_SCRUB].requirements}
    assert reqs["mechanical_mode"] == (F.MODE,)
    assert reqs["duration_or_protocol_reference"] == (F.EXPOSURE_SECONDS, F.PROTOCOL_SOURCE)


@pytest.mark.parametrize("family", [Family.SPA_BODY, Family.ACID_CARE])
def test_spa_and_acid_are_not_expressible_yet(family) -> None:
    assert REQUIREMENTS[family].expressible is False


def test_every_family_has_requirements() -> None:
    assert set(REQUIREMENTS) == set(Family.values)


# ─── исходы ──────────────────────────────────────────────────────────────────


def test_a_service_outside_body_care_is_not_subject(tenant, category) -> None:
    haircut = _offering(tenant, category, "Стрижка", family=None)
    no_canon = _offering(tenant, category, "Без канона", template=False)

    assert check_configuration(haircut).state == NOT_SUBJECT
    assert check_configuration(no_canon).state == NOT_SUBJECT


def test_a_wrap_with_no_facts_is_incomplete_and_names_everything(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание", family=Family.BODY_WRAP)

    check = check_configuration(wrap)

    assert check.state == INCOMPLETE
    assert set(check.missing) == {r.name for r in REQUIREMENTS[Family.BODY_WRAP].requirements}


def test_a_fully_known_wrap_is_complete(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание полное", family=Family.BODY_WRAP)
    _complete_wrap(wrap)

    check = check_configuration(wrap)

    assert check.state == COMPLETE
    assert check.missing == ()


def test_not_applicable_on_a_required_field_does_not_satisfy(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание без укрытия", family=Family.BODY_WRAP)
    _complete_wrap(wrap)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.COVERING_TYPE).update(
        state=St.NOT_APPLICABLE, value=None, source_ref="", source_type=None
    )

    check = check_configuration(wrap)

    assert check.state == INCOMPLETE
    assert check.missing == ("covering",)


def test_any_known_field_of_a_requirement_satisfies_it(tenant, category) -> None:
    """«Продукт» удовлетворяет и артикул, а не только название."""
    wrap = _offering(tenant, category, "Обёртывание по артикулу", family=Family.BODY_WRAP)
    _complete_wrap(wrap)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.PRODUCT_NAME).delete()
    _fact(wrap, F.PRODUCT_ARTICLE, value="ART-1")

    assert check_configuration(wrap).state == COMPLETE


@pytest.mark.parametrize("field", [F.EXPOSURE_SECONDS, F.PROTOCOL_SOURCE])
def test_a_scrub_is_complete_with_either_duration_or_protocol(tenant, category, field) -> None:
    scrub = _offering(tenant, category, f"Скраб {field}", family=Family.MECHANICAL_SCRUB)
    for f in (F.PRODUCT_NAME, F.APPLICATION_AREA, F.MODE, F.INSTRUCTION_VERSION, field):
        _fact(scrub, f)

    assert check_configuration(scrub).state == COMPLETE


def test_a_scrub_with_neither_duration_nor_protocol_is_incomplete(tenant, category) -> None:
    scrub = _offering(tenant, category, "Скраб без длительности", family=Family.MECHANICAL_SCRUB)
    for f in (F.PRODUCT_NAME, F.APPLICATION_AREA, F.MODE, F.INSTRUCTION_VERSION):
        _fact(scrub, f)

    check = check_configuration(scrub)

    assert check.state == INCOMPLETE
    assert check.missing == ("duration_or_protocol_reference",)


def test_a_conflict_is_its_own_signal_not_incomplete(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание спорное", family=Family.BODY_WRAP)
    _complete_wrap(wrap)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.EXPOSURE_SECONDS).update(
        state=St.CONFLICT, value=[1200, 1800], source_ref="", source_type=None
    )

    check = check_configuration(wrap)

    assert check.state == CONFLICT
    assert check.conflicts == (F.EXPOSURE_SECONDS,)


@pytest.mark.parametrize("family", [Family.SPA_BODY, Family.ACID_CARE])
def test_spa_and_acid_are_always_incomplete_with_a_reason(tenant, category, family) -> None:
    offering = _offering(tenant, category, f"Услуга {family}", family=family)
    for f in F.values:
        _fact(offering, f)

    check = check_configuration(offering)

    assert check.state == INCOMPLETE
    assert "не выразимы" in check.reason


# ─── пакетная форма ──────────────────────────────────────────────────────────


def test_the_pool_is_checked_in_two_queries(tenant, category, django_assert_num_queries) -> None:
    haircut = _offering(tenant, category, "Стрижка пула", family=None)
    wrap = _offering(tenant, category, "Обёртывание пула", family=Family.BODY_WRAP)
    _complete_wrap(wrap)
    scrub = _offering(tenant, category, "Скраб пула", family=Family.MECHANICAL_SCRUB)
    ids = [haircut.pk, wrap.pk, scrub.pk]

    with django_assert_num_queries(2):
        result = check_configurations(ids)

    assert {k: v.state for k, v in result.items()} == {
        haircut.pk: NOT_SUBJECT,
        wrap.pk: COMPLETE,
        scrub.pk: INCOMPLETE,
    }


def test_an_unknown_id_is_left_out_not_reported_as_not_subject(tenant, category) -> None:
    import uuid

    wrap = _offering(tenant, category, "Обёртывание одно", family=Family.BODY_WRAP)
    ghost = uuid.uuid4()

    result = check_configurations([wrap.pk, ghost])

    assert set(result) == {wrap.pk}
