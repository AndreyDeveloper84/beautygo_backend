"""Body Care §7A-1: вычисляемый кандидат юридического класса (DRF-2836).

Контракт v0.2 §7A.2; shape одобрен главным окном 06.10 (вычисляемый,
вариант a с оговоркой BASE).

Узлы держат:

* обёртывание и скраб — кандидат ``non_medical_cosmetic`` с провенансом
  ``system_derived``, правилом и версией правила;
* основание несёт видимую оговорку «BASE каноном не различается»;
* SPA и кислоты — кандидата нет, причина названа; кислоты — не
  ``legal_review_required``;
* канон вне body-care — ``subject=False``;
* кандидат НЕ пишет подтверждённое поле: после вызова
  ``legal_service_class`` остаётся ``NULL`` (позитивный контроль — поле
  записываемо, если его подтверждает человек);
* пакет — один запрос; неизвестный pk в ответ не попадает.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from services.body_care_legal import (
    BASE_CAVEAT,
    RULE,
    RULE_VERSION,
    SYSTEM_DERIVED,
    legal_class_candidate,
    legal_class_candidates,
)
from services.models import ServiceCategory, ServiceTemplate
from users.models import User

pytestmark = pytest.mark.django_db

Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Body care 7A-1", slug="bc-7a1")


def _canon(category, name, family=Family.BODY_WRAP) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=category,
        name=name,
        name_short=name[:40],
        service_family=family,
        canonical_version="1" if family else "",
    )


@pytest.mark.parametrize("family", [Family.BODY_WRAP, Family.MECHANICAL_SCRUB])
def test_wrap_and_scrub_get_a_non_medical_candidate(category, family) -> None:
    candidate = legal_class_candidate(_canon(category, f"Канон {family}", family))

    assert candidate.value == LC.NON_MEDICAL_COSMETIC
    assert candidate.subject is True
    assert candidate.provenance == SYSTEM_DERIVED
    assert (candidate.rule, candidate.rule_version) == (RULE, RULE_VERSION)
    assert RULE_VERSION


@pytest.mark.parametrize("family", [Family.BODY_WRAP, Family.MECHANICAL_SCRUB])
def test_the_basis_carries_the_base_caveat(category, family) -> None:
    """Человек, подтверждающий класс, видит, что BASE каноном не различается."""
    candidate = legal_class_candidate(_canon(category, f"Канон оговорки {family}", family))

    assert "§7A.2" in candidate.basis
    assert BASE_CAVEAT in candidate.basis


@pytest.mark.parametrize(
    ("family", "waits_for"),
    [(Family.SPA_BODY, "CAT-7"), (Family.ACID_CARE, "CAT-8")],
)
def test_spa_and_acid_get_no_candidate_with_a_reason(category, family, waits_for) -> None:
    candidate = legal_class_candidate(_canon(category, f"Канон без кандидата {family}", family))

    assert candidate.value is None
    assert candidate.subject is True
    assert waits_for in candidate.reason


def test_acid_is_not_marked_legal_review_required(category) -> None:
    """Без классификатора «класс неизвестен» неотличимо от «не проверяли»."""
    candidate = legal_class_candidate(_canon(category, "Кислоты", Family.ACID_CARE))

    assert candidate.value != LC.LEGAL_REVIEW_REQUIRED


def test_a_canon_outside_body_care_is_not_subject(category) -> None:
    candidate = legal_class_candidate(_canon(category, "Стрижка", family=None))

    assert candidate.subject is False
    assert candidate.value is None


def test_the_candidate_never_writes_the_confirmed_class(category) -> None:
    wrap = _canon(category, "Обёртывание не пишется")

    legal_class_candidates([wrap.pk])
    legal_class_candidate(wrap)
    wrap.refresh_from_db()

    assert wrap.legal_service_class is None


def test_positive_control_the_confirmed_class_is_writable_by_a_person(category) -> None:
    """Узел выше значим, только если поле вообще записываемо."""
    wrap = _canon(category, "Обёртывание подтверждённое")
    lawyer = User.objects.create_user(username="bc7a1-lawyer", password="x")
    ServiceTemplate.objects.filter(pk=wrap.pk).update(
        legal_service_class=LC.NON_MEDICAL_COSMETIC,
        legal_class_confirmed_by=lawyer,
        legal_class_confirmed_at=timezone.now(),
        legal_class_source_ref="решение владельца",
    )
    wrap.refresh_from_db()

    assert wrap.legal_service_class == LC.NON_MEDICAL_COSMETIC


def test_the_pool_is_read_in_one_query(category, django_assert_num_queries) -> None:
    ids = [
        _canon(category, "Пул обёртывание").pk,
        _canon(category, "Пул скраб", Family.MECHANICAL_SCRUB).pk,
        _canon(category, "Пул кислоты", Family.ACID_CARE).pk,
        _canon(category, "Пул стрижка", family=None).pk,
    ]

    with django_assert_num_queries(1):
        result = legal_class_candidates(ids)

    assert [result[i].value for i in ids] == [
        LC.NON_MEDICAL_COSMETIC, LC.NON_MEDICAL_COSMETIC, None, None,
    ]


def test_an_unknown_id_is_left_out(category) -> None:
    wrap = _canon(category, "Обёртывание одно")

    assert set(legal_class_candidates([wrap.pk, uuid.uuid4()])) == {wrap.pk}
