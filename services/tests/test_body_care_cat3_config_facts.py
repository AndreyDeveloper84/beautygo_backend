"""Body Care CAT-3: факты конфигурации предложения салона в пяти состояниях.

Контракт Body Care v0.1 §3.1 (поля конфигурации) и §4 (``null`` не может
быть единственным смыслом: KNOWN / UNKNOWN / NOT_APPLICABLE / NOT_PROVIDED /
CONFLICT). Решение владельца D-3: ``SalonService`` — это ``SalonOffering``
контракта; факты — отдельная таблица ``OfferingConfigFact``.

Узлы держат:

* словарь полей — ровно 19 полей §3.1, состояний — ровно пять §4;
* ``KNOWN`` — значение С ИСТОЧНИКОМ: без значения или без ``source_ref`` база
  отказывает (провенанс, §5);
* «не знаем / не применимо / не ответили» значения не несут — значение при
  ``UNKNOWN`` однажды прочли бы как известное;
* ``CONFLICT`` может нести расходящиеся варианты;
* один факт на поле у предложения;
* поле и состояние вне словаря не попадают в базу даже мимо ORM;
* **отсутствие строки читается как ``UNKNOWN``, а не как разрешение** (§4);
  опечатка в имени поля — ошибка, а не ``UNKNOWN``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from services.models import OfferingConfigFact, SalonService, ServiceCategory
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State


@pytest.fixture
def offering(db) -> SalonService:
    tenant = Tenant.objects.create(slug="cat3-salon", name="Салон CAT-3")
    category = ServiceCategory.objects.create(name="Обёртывания CAT-3", slug="cat3-wraps")
    return SalonService.objects.create(
        tenant=tenant,
        category=category,
        name="Шоколадное обёртывание",
        duration_minutes=60,
        base_price=Decimal("3500"),
    )


def _fact(offering, field=F.EXPOSURE_SECONDS, **fields) -> OfferingConfigFact:
    return OfferingConfigFact.objects.create(salon_service=offering, field=field, **fields)


# ─── словари ─────────────────────────────────────────────────────────────────


def test_the_field_vocabulary_is_exactly_the_contract_list() -> None:
    assert set(F.values) == {
        "product_name", "product_article", "manufacturer", "instruction_version",
        "instruction_region", "application_area", "application_area_size", "mode",
        "exposure_seconds", "application_count", "covering_type", "removal_method",
        "aftercare", "additional_modality", "heat_mode", "cold_mode",
        "compression_mode", "device_reference", "protocol_source",
    }


def test_the_state_vocabulary_is_exactly_the_five() -> None:
    assert {m.name for m in St} == {
        "KNOWN", "UNKNOWN", "NOT_APPLICABLE", "NOT_PROVIDED", "CONFLICT",
    }


def test_a_new_offering_has_no_configuration_version(offering) -> None:
    assert offering.configuration_version == ""


# ─── KNOWN — значение с источником ───────────────────────────────────────────


def test_known_with_value_and_source_is_accepted(offering) -> None:
    fact = _fact(
        offering,
        state=St.KNOWN,
        source_type="manufacturer_instruction",
        value=900,
        source_ref="инструкция производителя v3",
    )

    fact.refresh_from_db()
    assert (fact.state, fact.value) == (St.KNOWN, 900)


def test_known_without_a_source_is_refused(offering) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_known_requires_value_and_source"):
        with transaction.atomic():
            _fact(offering, state=St.KNOWN, source_type="manufacturer_instruction", value=900)


def test_known_without_a_value_is_refused(offering) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_known_requires_value_and_source"):
        with transaction.atomic():
            _fact(offering, state=St.KNOWN, source_type="manufacturer_instruction", source_ref="инструкция")


# ─── «не знаем / не применимо / не ответили» — без значения ─────────────────


@pytest.mark.parametrize("state", [St.UNKNOWN, St.NOT_APPLICABLE, St.NOT_PROVIDED])
def test_absent_states_are_accepted_without_a_value(offering, state) -> None:
    fact = _fact(offering, state=state)

    assert fact.value is None


@pytest.mark.parametrize("state", [St.UNKNOWN, St.NOT_APPLICABLE, St.NOT_PROVIDED])
def test_absent_states_refuse_a_value(offering, state) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_absent_states_carry_no_value"):
        with transaction.atomic():
            _fact(offering, state=state, value=900)


def test_a_conflict_may_carry_the_diverging_values(offering) -> None:
    fact = _fact(offering, state=St.CONFLICT, value=[600, 900])

    fact.refresh_from_db()
    assert fact.value == [600, 900]


# ─── форма таблицы ───────────────────────────────────────────────────────────


def test_one_fact_per_field_per_offering(offering) -> None:
    _fact(offering, state=St.UNKNOWN)

    with pytest.raises(IntegrityError, match="offeringconfigfact_one_fact_per_field"):
        with transaction.atomic():
            _fact(offering, state=St.KNOWN, source_type="manufacturer_instruction", value=900, source_ref="инструкция")


def test_an_unknown_field_does_not_reach_the_database_even_past_the_orm(offering) -> None:
    fact = _fact(offering, state=St.UNKNOWN)

    with pytest.raises(IntegrityError, match="offeringconfigfact_field_known"):
        with transaction.atomic():
            OfferingConfigFact.objects.filter(pk=fact.pk).update(field="temperature")


def test_an_unknown_state_does_not_reach_the_database_even_past_the_orm(offering) -> None:
    fact = _fact(offering, state=St.UNKNOWN)

    with pytest.raises(IntegrityError, match="offeringconfigfact_state_known"):
        with transaction.atomic():
            OfferingConfigFact.objects.filter(pk=fact.pk).update(state="maybe")


def test_facts_go_with_their_offering(offering) -> None:
    _fact(offering, state=St.UNKNOWN)

    offering.delete()

    assert OfferingConfigFact.objects.count() == 0


# ─── чтение: отсутствие — это UNKNOWN, не разрешение ────────────────────────


def test_a_missing_fact_reads_as_unknown_not_as_permission(offering) -> None:
    assert offering.config_fact(F.EXPOSURE_SECONDS) == (St.UNKNOWN, None)


def test_a_present_fact_reads_as_itself(offering) -> None:
    _fact(offering, field=F.HEAT_MODE, state=St.NOT_APPLICABLE)
    _fact(offering, state=St.KNOWN, source_type="manufacturer_instruction", value=900, source_ref="инструкция")

    assert offering.config_fact(F.HEAT_MODE) == (St.NOT_APPLICABLE, None)
    assert offering.config_fact(F.EXPOSURE_SECONDS) == (St.KNOWN, 900)


def test_a_typo_in_the_field_name_is_an_error_not_unknown(offering) -> None:
    with pytest.raises(ValueError, match="unknown configuration field"):
        offering.config_fact("exposure_second")
