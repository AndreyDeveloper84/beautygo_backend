"""Body Care CAT-4: провенанс факта конфигурации предложения (контракт §5).

Контракт §5: «каждый safety-relevant fact должен иметь provenance» — вид
источника (семь значений), ссылка, версия, когда и кем зафиксирован.

Узлы держат:

* вид источника — ровно семь значений §5, и вне их в базу ничего не попадает;
* ``KNOWN`` без вида источника — отказ: «инструкция производителя» и «так
  сказал салон» должны различаться для того, кто решает о безопасности;
* на «отсутствующих» состояниях вид источника пуст законно и «проверено» не
  означает;
* кто зафиксировал — «кто ИЛИ правило», правило с версией (форма соседних
  акторов провенанса этого репозитория);
* перепись удаления человека решает новый указатель на ``User`` (RETAIN);
* канарейка: известные факты с ``SYSTEM_DERIVED`` видны числом — не гейт и не
  нарушение (какие поля его запрещают, решает клиника, D-4).

``confidence`` из §5 не введён: шкала не задана контрактом (вопрос
владельцу Q1).
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import OfferingConfigFact, SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

F = OfferingConfigFact.Field
St = OfferingConfigFact.State
Src = OfferingConfigFact.SourceType


@pytest.fixture
def offering(db) -> SalonService:
    tenant = Tenant.objects.create(slug="cat4-salon", name="Салон CAT-4")
    category = ServiceCategory.objects.create(name="Скрабы CAT-4", slug="cat4-scrubs")
    return SalonService.objects.create(
        tenant=tenant,
        category=category,
        name="Солевой скраб",
        duration_minutes=45,
        base_price=Decimal("2500"),
    )


@pytest.fixture
def curator(db) -> User:
    return User.objects.create_user(username="cat4-curator", password="x")


def _known(offering, **fields) -> OfferingConfigFact:
    base = dict(
        salon_service=offering,
        field=F.EXPOSURE_SECONDS,
        state=St.KNOWN,
        value=600,
        source_ref="инструкция производителя, раздел 4",
        source_type=Src.MANUFACTURER_INSTRUCTION,
    )
    base.update(fields)
    return OfferingConfigFact.objects.create(**base)


# ─── вид источника ───────────────────────────────────────────────────────────


def test_the_source_vocabulary_is_exactly_the_contract_seven() -> None:
    assert {m.name for m in Src} == {
        "MANUFACTURER_INSTRUCTION",
        "PROTOCOL_DOCUMENT",
        "OWNER_INPUT",
        "SALON_INPUT",
        "PHYSICIAN_POLICY",
        "CANONICAL_POLICY",
        "SYSTEM_DERIVED",
    }


def test_known_with_the_full_provenance_is_accepted(offering, curator) -> None:
    fact = _known(
        offering,
        source_version="v3 (2025)",
        captured_at=timezone.now(),
        captured_by=curator,
    )

    fact.refresh_from_db()
    assert (fact.source_type, fact.source_version, fact.captured_by_id) == (
        Src.MANUFACTURER_INSTRUCTION,
        "v3 (2025)",
        curator.pk,
    )


def test_known_without_a_source_type_is_refused(offering) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_known_requires_source_type"):
        with transaction.atomic():
            _known(offering, source_type=None)


@pytest.mark.parametrize("state", [St.UNKNOWN, St.NOT_APPLICABLE, St.NOT_PROVIDED])
def test_absent_states_carry_no_source_type_legitimately(offering, state) -> None:
    fact = OfferingConfigFact.objects.create(salon_service=offering, field=F.MODE, state=state)

    assert fact.source_type is None


def test_an_unknown_source_type_does_not_reach_the_database_even_past_the_orm(offering) -> None:
    fact = _known(offering)

    with pytest.raises(IntegrityError, match="offeringconfigfact_source_type_known"):
        with transaction.atomic():
            OfferingConfigFact.objects.filter(pk=fact.pk).update(source_type="hearsay")


# ─── кто зафиксировал: кто ИЛИ правило ──────────────────────────────────────


def test_captured_by_a_rule_with_version_is_accepted(offering) -> None:
    fact = _known(offering, captured_rule="manufacturer-sheet-import", capture_rule_version="1")

    assert fact.captured_rule == "manufacturer-sheet-import"


def test_who_and_rule_together_are_refused(offering, curator) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_capture_is_who_xor_rule"):
        with transaction.atomic():
            _known(
                offering,
                captured_by=curator,
                captured_rule="manufacturer-sheet-import",
                capture_rule_version="1",
            )


def test_a_capture_rule_without_version_is_refused(offering) -> None:
    with pytest.raises(IntegrityError, match="offeringconfigfact_capture_rule_carries_version"):
        with transaction.atomic():
            _known(offering, captured_rule="manufacturer-sheet-import")


# ─── перепись удаления человека ─────────────────────────────────────────────


def test_the_erasure_census_decides_the_new_pointer() -> None:
    from users.deletion_executor import RETAIN

    assert "services.OfferingConfigFact.captured_by" in RETAIN


# ─── канарейка SYSTEM_DERIVED — число, не нарушение ─────────────────────────


def _report(**options) -> str:
    out = StringIO()
    call_command("check_canon_invariants", stdout=out, **options)
    return out.getvalue()


def test_known_system_derived_facts_are_counted(offering) -> None:
    _known(offering, source_type=Src.SYSTEM_DERIVED)
    _known(offering, field=F.HEAT_MODE, value="off")  # инструкция — не считается

    assert "known_system_derived_facts : 1" in _report()


def test_an_unknown_system_derived_fact_is_not_counted(offering) -> None:
    OfferingConfigFact.objects.create(
        salon_service=offering, field=F.MODE, state=St.UNKNOWN, source_type=Src.SYSTEM_DERIVED
    )

    assert "known_system_derived_facts : 0" in _report()


def test_the_canary_does_not_fail_the_strict_run(offering) -> None:
    """Канарейка — наблюдаемость, не гейт: строгий прогон из-за неё не падает."""
    _known(offering, source_type=Src.SYSTEM_DERIVED)

    report = _report(fail_on_violations=True)

    assert "known_system_derived_facts : 1" in report
