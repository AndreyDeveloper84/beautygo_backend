"""Body Care CAT-6: состояние валидации предложения (контракт §7) и ревью конфигурации.

Решения (главное окно, 06.10): READY только при явном ревью конфигурации,
привязанном к её версии (В1); состояние вычисляется, хранится только ревью
(В2); лестница RETIRED > BLOCKED > INCOMPLETE > REVIEW_REQUIRED > READY (В3);
вне body-care — маркер ``not_subject``, ``None`` не возвращается (В4).

Узлы держат:

* словарь — ровно пять состояний §7, маркер ``not_subject`` вне его;
* услуга вне body-care — ``not_subject`` (CAT-10 её пропускает);
* канон ``retired`` — ``retired`` даже при полной и проверенной конфигурации;
* противоречие — ``blocked``; не хватает фактов — ``incomplete``;
* полная конфигурация без ревью — ``review_required``, не READY;
* ревью той же версии — ``ready_for_screening``; изменили конфигурацию —
  ревью устарело, снова ``review_required``;
* отпечаток фактов: правка значения, замена источника, новая строка после
  ревью — ``review_required`` без подъёма версии; отпечаток покрывает
  каждую колонку факта;
* ревью — решение с провенансом (CheckConstraint'ы), перепись удаления его
  решает;
* пул — два запроса; неизвестный ``pk`` в ответ не попадает.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.body_care_validation import (
    BLOCKED,
    INCOMPLETE,
    NOT_SUBJECT,
    READY_FOR_SCREENING,
    RETIRED,
    REVIEW_REQUIRED,
    FINGERPRINT_COLUMNS,
    VALIDATION_STATES,
    config_fingerprint,
    record_config_review,
    validation_state,
    validation_states,
)
from services.models import OfferingConfigFact, SalonService, ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State
Family = ServiceTemplate.ServiceFamily
L = ServiceTemplate.Lifecycle

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
def tenant(db):
    return Tenant.objects.create(slug="cat6-salon", name="Салон CAT-6")


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care CAT-6", slug="cat6-body")


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="cat6-curator", password="x")


def _offering(
    tenant, category, name, family=Family.BODY_WRAP, legal_class="non_medical_cosmetic", **template_fields
) -> SalonService:
    """По умолчанию юридический класс подтверждён немедицинским: с §7A-6 без
    подтверждённого класса предложение не бывает READY, а узлы этого файла —
    про конфигурацию и ревью. Узлы §7A-6 передают свой класс или ``None``."""
    tpl = ServiceTemplate.objects.create(
        category=category,
        name=f"Канон {name}",
        name_short=name[:40],
        service_family=family,
        canonical_version="1" if family else "",
        **template_fields,
    )
    if legal_class is not None and family:
        lawyer, _ = User.objects.get_or_create(username="cat6-lawyer")
        ServiceTemplate.objects.filter(pk=tpl.pk).update(
            legal_service_class=legal_class,
            legal_class_confirmed_by=lawyer,
            legal_class_confirmed_at=timezone.now(),
            legal_class_source_ref="решение юриста",
        )
    return SalonService.objects.create(
        tenant=tenant,
        category=category,
        template=tpl,
        name=name,
        duration_minutes=60,
        base_price=Decimal("3000"),
        configuration_version="cfg-1",
    )


def _complete(offering) -> None:
    for field, value in WRAP.items():
        OfferingConfigFact.objects.create(
            salon_service=offering,
            field=field,
            state=St.KNOWN,
            value=value,
            source_ref="инструкция",
            source_type="manufacturer_instruction",
        )


def _review(offering, curator) -> None:
    record_config_review(offering, by=curator, source_ref="ревью куратора")


# ─── словарь ─────────────────────────────────────────────────────────────────


def test_the_states_are_exactly_the_five_of_section_seven() -> None:
    assert set(VALIDATION_STATES) == {
        "incomplete", "review_required", "ready_for_screening", "blocked", "retired",
    }
    assert NOT_SUBJECT not in VALIDATION_STATES


# ─── лестница ────────────────────────────────────────────────────────────────


def test_a_service_outside_body_care_is_not_subject(tenant, category) -> None:
    haircut = _offering(tenant, category, "Стрижка", family=None)

    assert validation_state(haircut) == NOT_SUBJECT


def test_a_retired_canon_wins_over_a_complete_reviewed_configuration(
    tenant, category, curator
) -> None:
    wrap = _offering(
        tenant,
        category,
        "Обёртывание выведенное",
        lifecycle=L.RETIRED,
        retired_by=curator,
        retired_at=timezone.now(),
        retirement_source_ref="реестр",
    )
    _complete(wrap)
    _review(wrap, curator)

    assert validation_state(wrap) == RETIRED


def test_a_conflict_blocks(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание спорное")
    _complete(wrap)
    _review(wrap, curator)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.EXPOSURE_SECONDS).update(
        state=St.CONFLICT, value=[1200, 1800], source_ref="", source_type=None
    )

    assert validation_state(wrap) == BLOCKED


def test_missing_facts_are_incomplete_even_with_a_review(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание неполное")
    _review(wrap, curator)

    assert validation_state(wrap) == INCOMPLETE


def test_a_complete_configuration_without_review_is_not_ready(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание без ревью")
    _complete(wrap)

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_a_reviewed_complete_configuration_is_ready(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание проверенное")
    _complete(wrap)
    _review(wrap, curator)

    assert validation_state(wrap) == READY_FOR_SCREENING


def test_changing_the_configuration_makes_the_review_stale(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание изменённое")
    _complete(wrap)
    _review(wrap, curator)
    SalonService.objects.filter(pk=wrap.pk).update(configuration_version="cfg-2")

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_an_unversioned_configuration_is_never_ready(tenant, category) -> None:
    """Пустая версия конфигурации не «совпадает» с пустой версией ревью —
    даже когда записанный отпечаток совпал бы с фактами."""
    wrap = _offering(tenant, category, "Обёртывание без версии")
    _complete(wrap)
    rows = OfferingConfigFact.objects.filter(salon_service=wrap).values(*FINGERPRINT_COLUMNS)
    SalonService.objects.filter(pk=wrap.pk).update(
        configuration_version="", config_reviewed_fingerprint=config_fingerprint(rows)
    )

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_an_unversioned_configuration_cannot_be_reviewed(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание без версии для ревью")
    SalonService.objects.filter(pk=wrap.pk).update(configuration_version="")

    with pytest.raises(ValueError, match="без версии"):
        _review(wrap, curator)


# ─── отпечаток: любая правка факта делает ревью неактуальным ────────────────


def test_editing_a_fact_value_after_review_makes_it_stale(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание с новой экспозицией")
    _complete(wrap)
    _review(wrap, curator)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.EXPOSURE_SECONDS).update(value=2400)

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_replacing_a_facts_source_after_review_makes_it_stale(tenant, category, curator) -> None:
    """Замена источника при том же значении — тоже правка конфигурации."""
    wrap = _offering(tenant, category, "Обёртывание с новой инструкцией")
    _complete(wrap)
    _review(wrap, curator)
    OfferingConfigFact.objects.filter(salon_service=wrap, field=F.EXPOSURE_SECONDS).update(
        source_ref="инструкция v4", source_version="4"
    )

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_adding_a_fact_after_review_makes_it_stale(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Обёртывание с уходом")
    _complete(wrap)
    _review(wrap, curator)
    OfferingConfigFact.objects.create(
        salon_service=wrap,
        field=F.AFTERCARE,
        state=St.KNOWN,
        value="крем",
        source_ref="инструкция",
        source_type="manufacturer_instruction",
    )

    assert validation_state(wrap) == REVIEW_REQUIRED


def test_the_fingerprint_does_not_depend_on_row_order(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание порядок")
    _complete(wrap)
    rows = list(OfferingConfigFact.objects.filter(salon_service=wrap).values(*FINGERPRINT_COLUMNS))

    assert config_fingerprint(rows) == config_fingerprint(reversed(rows))


def test_the_fingerprint_covers_every_fact_column() -> None:
    """Новая колонка факта, забытая в отпечатке, правилась бы мимо ревью."""
    columns = {f.attname for f in OfferingConfigFact._meta.concrete_fields}

    assert set(FINGERPRINT_COLUMNS) == columns - {"id", "salon_service_id"}


# ─── ревью — решение с провенансом ──────────────────────────────────────────


def test_a_review_without_provenance_is_refused(tenant, category) -> None:
    wrap = _offering(tenant, category, "Ревью без основания")

    with pytest.raises(IntegrityError, match="salonservice_config_review_requires_provenance"):
        with transaction.atomic():
            SalonService.objects.filter(pk=wrap.pk).update(config_reviewed_version="cfg-1")


def test_review_by_person_and_rule_together_is_refused(tenant, category, curator) -> None:
    wrap = _offering(tenant, category, "Ревью кто и правило")

    with pytest.raises(IntegrityError, match="salonservice_config_review_is_who_xor_rule"):
        with transaction.atomic():
            SalonService.objects.filter(pk=wrap.pk).update(
                config_reviewed_by=curator,
                config_review_rule="auto",
                config_review_rule_version="1",
                config_reviewed_at=timezone.now(),
                config_review_source_ref="x",
                config_reviewed_version="cfg-1",
                config_reviewed_fingerprint="f" * 64,
            )


def test_a_review_rule_without_version_is_refused(tenant, category) -> None:
    wrap = _offering(tenant, category, "Ревью правилом без версии")

    with pytest.raises(IntegrityError, match="salonservice_config_review_rule_carries_version"):
        with transaction.atomic():
            SalonService.objects.filter(pk=wrap.pk).update(
                config_review_rule="auto",
                config_reviewed_at=timezone.now(),
                config_review_source_ref="x",
                config_reviewed_version="cfg-1",
                config_reviewed_fingerprint="f" * 64,
            )


def test_a_review_without_fingerprint_is_refused(tenant, category, curator) -> None:
    """Ревью мимо ``record_config_review`` без отпечатка база не примет."""
    wrap = _offering(tenant, category, "Ревью без отпечатка")

    with pytest.raises(IntegrityError, match="salonservice_config_review_requires_provenance"):
        with transaction.atomic():
            SalonService.objects.filter(pk=wrap.pk).update(
                config_reviewed_by=curator,
                config_reviewed_at=timezone.now(),
                config_review_source_ref="x",
                config_reviewed_version="cfg-1",
            )


def test_the_erasure_census_decides_the_reviewer_pointer() -> None:
    from users.deletion_executor import RETAIN

    assert "services.SalonService.config_reviewed_by" in RETAIN


# ─── пакетная форма для CAT-10 ──────────────────────────────────────────────


def test_the_pool_is_read_in_three_queries(tenant, category, curator, django_assert_num_queries) -> None:
    """Предложения, факты и лицензии салонов (§7A-6) — по одному запросу на пул."""
    haircut = _offering(tenant, category, "Стрижка пула", family=None)
    ready = _offering(tenant, category, "Обёртывание пула готовое")
    _complete(ready)
    _review(ready, curator)
    bare = _offering(tenant, category, "Обёртывание пула пустое")
    ids = [haircut.pk, ready.pk, bare.pk]

    with django_assert_num_queries(3):
        result = validation_states(ids)

    assert result == {haircut.pk: NOT_SUBJECT, ready.pk: READY_FOR_SCREENING, bare.pk: INCOMPLETE}


def test_no_value_is_ever_none(tenant, category) -> None:
    _offering(tenant, category, "Стрижка без None", family=None)
    _offering(tenant, category, "Обёртывание без None")

    values = validation_states(SalonService.objects.values_list("pk", flat=True)).values()

    assert None not in values
    assert set(values) <= set(VALIDATION_STATES) | {NOT_SUBJECT}


def test_an_unknown_id_is_left_out(tenant, category) -> None:
    wrap = _offering(tenant, category, "Обёртывание одно")

    assert set(validation_states([wrap.pk, uuid.uuid4()])) == {wrap.pk}
