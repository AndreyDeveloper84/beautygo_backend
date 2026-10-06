"""Body Care CAT-1: семейство и версии канона на ``ServiceTemplate``.

Контракт Body Care v0.1 §2.1 требует у канонической услуги семейство
(``BODY_WRAP`` / ``SPA_BODY`` / ``MECHANICAL_SCRUB`` / ``ACID_CARE``),
версию канона, версии клинической политики и политики владельца и
юридический статус. Решение владельца D-3 (06.10) — расширять
``ServiceTemplate``, а не заводить новую сущность.

Узлы держат то, что из этого следует:

* услуга вне body-care (стрижка, маникюр) семейства не имеет — ``NULL``, а не
  «прочее», и ничего нового от неё не требуется;
* назначенное семейство без версии канона — отказ базы: решение о
  безопасности по канону без версии нельзя воспроизвести;
* в базу не попадает семейство вне четырёх значений контракта, даже мимо ORM;
* у ``legal_status`` нет ни словаря, ни умолчания: семантика ждёт юриста
  (D-1), ``NULL`` = «не определён»;
* клиническая версия не обязательна — канон можно завести до клиники.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from services.models import ServiceCategory, ServiceTemplate

pytestmark = pytest.mark.django_db

Family = ServiceTemplate.ServiceFamily


def _tpl(name: str, **fields) -> ServiceTemplate:
    category = ServiceCategory.objects.get_or_create(name="Body care CAT-1")[0]
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], **fields
    )


def test_the_family_vocabulary_is_exactly_the_contract_four() -> None:
    assert {member.name for member in Family} == {
        "BODY_WRAP",
        "SPA_BODY",
        "MECHANICAL_SCRUB",
        "ACID_CARE",
    }


def test_a_service_outside_body_care_needs_nothing_new() -> None:
    tpl = _tpl("Стрижка CAT-1")

    tpl.refresh_from_db()
    assert tpl.service_family is None
    assert (tpl.canonical_version, tpl.clinical_policy_version, tpl.owner_policy_version) == (
        "",
        "",
        "",
    )
    assert tpl.legal_status is None


def test_a_family_without_a_canonical_version_is_refused() -> None:
    with pytest.raises(IntegrityError, match="servicetemplate_family_requires_canonical_version"):
        with transaction.atomic():
            _tpl("Обёртывание без версии", service_family=Family.BODY_WRAP)


@pytest.mark.parametrize("family", list(Family))
def test_each_family_with_a_canonical_version_is_accepted(family) -> None:
    tpl = _tpl(f"Body care {family.name}", service_family=family, canonical_version="1.0.0")

    tpl.refresh_from_db()
    assert tpl.service_family == family
    assert tpl.canonical_version == "1.0.0"


def test_the_clinical_policy_version_is_not_required() -> None:
    """Канон заводится до утверждения клинической политики (D-2/D-4)."""
    tpl = _tpl("Скраб CAT-1", service_family=Family.MECHANICAL_SCRUB, canonical_version="0.1")

    assert tpl.clinical_policy_version == ""


def test_an_unknown_family_does_not_reach_the_database_even_past_the_orm() -> None:
    tpl = _tpl("SPA CAT-1", service_family=Family.SPA_BODY, canonical_version="1")

    with pytest.raises(IntegrityError, match="servicetemplate_service_family_known"):
        with transaction.atomic():
            ServiceTemplate.objects.filter(pk=tpl.pk).update(service_family="other")


def test_removing_the_version_from_a_family_service_is_refused_too() -> None:
    tpl = _tpl("Кислота CAT-1", service_family=Family.ACID_CARE, canonical_version="2")

    with pytest.raises(IntegrityError, match="servicetemplate_family_requires_canonical_version"):
        with transaction.atomic():
            ServiceTemplate.objects.filter(pk=tpl.pk).update(canonical_version="")


def test_legal_status_has_no_vocabulary_until_the_lawyer_rules() -> None:
    """D-1 не принято: поле хранит то, что запишут, а не выбор из придуманного словаря."""
    field = ServiceTemplate._meta.get_field("legal_status")

    assert field.null is True
    assert not field.choices
