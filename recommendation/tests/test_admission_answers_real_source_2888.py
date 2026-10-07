"""DRF-2888 — настоящий источник отдаёт ответ КАЖДОЙ проверки допуска.

До этого наружу уходило только первое несошедшееся из трёх чтений §7A. Здесь
— на настоящих строках и настоящих функциях каталога:

- у кандидата восемь ответов, в порядке списка;
- проверки, стоящие ПОСЛЕ несошедшейся, отвечают сами, а не молчат: мастер
  без лицензии салона и без квалификации несёт оба отказа;
- свёрнутые гейты и набор ответов называют одну и ту же первую причину — и
  её же отдаёт S1.
"""
from __future__ import annotations

import pytest

from recommendation._admission import ALL_CHECKS, AdmissionCheck, CheckOutcome, first_unmet
from recommendation._reason_codes import ReasonCode
from recommendation._types import ConfigGate, LegalGate
from recommendation.tests.test_admission_checks_2888 import _chain_of_ifs
from recommendation.tests.test_legal_gate_cat10_ext_2843 import (  # noqa: F401 — фикстуры
    LC,
    PC,
    _does,
    _excluded,
    _fetch,
    _license,
    _master,
    _offer,
    _peel,
    _place,
    _qualify,
    _resolve,
    category,
    curator,
    tenant,
)

pytestmark = pytest.mark.django_db

A = AdmissionCheck
O = CheckOutcome  # noqa: E741


def _outcomes(facts) -> dict[AdmissionCheck, CheckOutcome]:
    assert facts.admission is not None
    assert [a.check for a in facts.admission] == list(ALL_CHECKS)
    return {a.check: a.outcome for a in facts.admission}


def test_a_plain_offer_passes_what_applies_and_the_rest_does_not_apply(tenant, category, curator):  # noqa: F811
    master = _master(tenant, "61")
    _does(master, _offer(tenant, category, curator, name="Стрижка"))

    [facts] = _fetch()

    outcomes = _outcomes(facts)
    assert outcomes[A.MAPPING] is O.PASSED and outcomes[A.CANON_RETIRED] is O.PASSED
    assert outcomes[A.CONFIG] is O.NOT_APPLICABLE, "канон вне Body Care"
    assert O.FAILED not in outcomes.values() and O.UNDETERMINED not in outcomes.values()
    assert first_unmet(facts.admission) is None


def test_everything_in_place_is_passed_by_name(tenant, category, curator):  # noqa: F811
    place = _place(tenant, curator)
    master = _master(tenant, "62", place=place)
    peel = _peel(tenant, category, curator)
    _does(master, peel)
    _license(tenant, curator, covers=[peel.template], places=[place])
    _qualify(master, curator)

    [facts] = _fetch()

    outcomes = _outcomes(facts)
    assert {outcomes[c] for c in (A.LEGAL_CLASS, A.LICENSE, A.ADDRESS, A.QUALIFICATION)} == {O.PASSED}


def test_the_checks_after_the_first_failure_still_answer(tenant, category, curator):  # noqa: F811
    """Лицензии у салона нет И квалификации у мастера нет. Раньше наружу уходило только первое;
    теперь видны оба отказа — этим и отвечают на вопрос «что закрывает каждая проверка»."""
    master = _master(tenant, "63", place=_place(tenant, curator))
    _does(master, _peel(tenant, category, curator))

    [facts] = _fetch()

    answers = {a.check: a for a in facts.admission}
    assert facts.legal_gate is LegalGate.LICENSE_NOT_VERIFIED, "свёрнутый гейт называет только первое"
    assert answers[A.LICENSE].reason is ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED
    assert answers[A.QUALIFICATION].reason is ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED
    assert answers[A.ADDRESS].outcome is O.NOT_APPLICABLE, "без покрывающей лицензии адрес сверять не с чем"
    assert first_unmet(facts.admission).check is A.LICENSE
    assert first_unmet(facts.admission, enabled=[A.QUALIFICATION]).check is A.QUALIFICATION


def test_a_class_under_legal_review_is_one_failure(tenant, category, curator):  # noqa: F811
    master = _master(tenant, "64")
    _does(master, _offer(tenant, category, curator, name="Спорная", legal_class=LC.LEGAL_REVIEW_REQUIRED))

    [facts] = _fetch()

    outcomes = _outcomes(facts)
    assert outcomes[A.LEGAL_CLASS] is O.FAILED
    assert (outcomes[A.LICENSE], outcomes[A.ADDRESS]) == (O.NOT_APPLICABLE, O.NOT_APPLICABLE)


def test_the_answers_the_collapsed_gates_and_s1_name_the_same_first_reason(tenant, category, curator):  # noqa: F811
    """Пул из разных состояний: по каждому кандидату три свидетеля сходятся."""
    place = _place(tenant, curator)
    plain, no_licence, unqualified, review, cleared = (_master(tenant, f"7{i}", place=place) for i in range(5))
    _does(plain, _offer(tenant, category, curator, name="Стрижка"))
    _does(no_licence, _offer(
        tenant, category, curator, name="Вне объёма лицензии", legal_class=LC.MEDICAL_OTHER,
        required=PC.PHYSICIAN_COSMETOLOGIST,
    ))
    _does(review, _offer(tenant, category, curator, name="Спорная", legal_class=LC.LEGAL_REVIEW_REQUIRED))
    covered = _peel(tenant, category, curator, name="Пилинг покрытый")
    _does(unqualified, covered)
    _does(cleared, covered)
    _license(tenant, curator, covers=[covered.template], places=[place])
    _qualify(cleared, curator)

    excluded = _excluded(_resolve())
    seen = set()
    for facts in _fetch():
        from_answers = first_unmet(facts.admission)
        from_gates = _chain_of_ifs(facts.mapping_status, facts.canon_retired, facts.config_gate, facts.legal_gate)
        from_s1 = excluded.get(str(facts.ref.id))
        assert (None if from_answers is None else from_answers.reason) is from_gates is from_s1
        seen.add(from_s1)

    assert seen == {
        None,
        # У салона проверенная лицензия есть, но этот канон в её объём не входит.
        ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH,
        ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED,
        ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
    }, "контроль: пул действительно разный, а не пять одинаковых ответов"


def test_a_candidate_with_only_a_legacy_offer_carries_no_answers():
    """Легаси-строка канонической связи не имеет — судит статус связи (см. источник)."""
    from users.recommendation_source import _MappingFacts

    assert _MappingFacts().admission(has_offer=True, matched_service_ref=None) is None


def test_the_config_gate_of_a_plain_offer_is_what_the_answers_say(tenant, category, curator):  # noqa: F811
    master = _master(tenant, "65")
    _does(master, _offer(tenant, category, curator, name="Стрижка"))

    [facts] = _fetch()

    assert facts.config_gate is ConfigGate.NOT_SUBJECT
    assert _outcomes(facts)[A.SCOPE] is O.PASSED
