"""DRF-2888 — проверки допуска каталога именованным списком.

Что заперто:

- список закрыт и упорядочен: ровно восемь проверок допуска, и ни одной
  проверки безопасности, области запроса или согласий среди них;
- кандидат несёт ответ КАЖДОЙ проверки; одна причина — одна несошедшаяся
  проверка (класс не подтверждён не выглядит тремя отказами);
- «не действует» (``NOT_ENFORCED``) нигде не превращается в «пройдено», а
  строгая свёртка возвращает её с её собственным исходом;
- S1 через этот список отвечает теми же кодами, что отвечала цепочка условий:
  по ВСЕМ сочетаниям значений гейтов;
- набор проверок меньше полного — только через политику стадий: с
  отключённой проверкой связи кандидат проходит без свидетельства «связь
  подтверждена».
"""
from __future__ import annotations

import itertools

import pytest

from recommendation._admission import (
    ALL_CHECKS,
    UNENFORCED_LITERALS,
    UNMET_OUTCOMES,
    AdmissionCheck,
    CheckAnswer,
    CheckOutcome,
    answers_from_collapsed_gates,
    build_answers,
    first_unmet,
)
from recommendation._reason_codes import GATE_EXCLUSION_CODES, ReasonCode
from recommendation._stages import StagePolicy, apply_eligibility
from recommendation._types import ConfigGate, LegalGate, MappingStatus
from recommendation.tests.conftest import make_facts, make_request

A = AdmissionCheck
O = CheckOutcome  # noqa: E741 — короткое имя ради таблиц ниже


def _built(**over) -> dict[AdmissionCheck, CheckAnswer]:
    """Ответы по строке, у которой всё сошлось; ``over`` портит ровно одно."""
    args = dict(
        mapping_status=MappingStatus.VERIFIED, canon_retired=False, config_gate=ConfigGate.READY,
        license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
        license_verified=True, address_verified=True, qualification_verified=True,
    )
    args.update(over)
    answers = build_answers(**args)
    assert [a.check for a in answers] == list(ALL_CHECKS), "восемь ответов, в порядке списка"
    return {a.check: a for a in answers}


def _outcomes(**over) -> dict[AdmissionCheck, CheckOutcome]:
    return {check: answer.outcome for check, answer in _built(**over).items()}


def _unmet(**over) -> list[AdmissionCheck]:
    return [check for check, outcome in _outcomes(**over).items() if outcome in UNMET_OUTCOMES]


class TestTheListIsClosed:
    def test_exactly_eight_checks_in_the_order_s1_reads_them(self):
        assert [check.value for check in ALL_CHECKS] == [
            "MAPPING", "CANON_RETIRED", "SCOPE", "CONFIG", "LEGAL_CLASS", "LICENSE", "ADDRESS", "QUALIFICATION",
        ]

    @pytest.mark.parametrize("word", ["SAFETY", "HEALTH", "TENANT", "SALON", "CONSENT", "BUDGET", "PREFERENCE", "DEMO"])
    def test_nothing_about_safety_isolation_or_consent_can_be_switched_off_here(self, word):
        """Решение владельца: личность, права, изоляция салонов и согласия сохраняются."""
        assert not [check for check in AdmissionCheck if word in check.value]

    def test_the_catalog_literals_for_unenforced_checks_are_known_by_name(self):
        assert UNENFORCED_LITERALS == {
            "scope": A.SCOPE, "legal_class": A.LEGAL_CLASS, "license": A.LICENSE,
            "address": A.ADDRESS, "qualification": A.QUALIFICATION,
        }

    def test_an_answer_carries_a_reason_exactly_when_it_is_unmet(self):
        with pytest.raises(ValueError):
            CheckAnswer(A.MAPPING, O.FAILED)
        with pytest.raises(ValueError):
            CheckAnswer(A.MAPPING, O.PASSED, ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE)
        with pytest.raises(ValueError):
            CheckAnswer(A.SCOPE, O.NOT_ENFORCED, ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED)


class TestEveryCheckAnswers:
    def test_a_row_where_everything_is_in_place(self):
        assert set(_outcomes().values()) == {O.PASSED}

    @pytest.mark.parametrize(
        ("spoil", "check", "reason"),
        [
            ({"mapping_status": MappingStatus.UNMAPPED}, A.MAPPING, ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE),
            ({"canon_retired": True}, A.CANON_RETIRED, ReasonCode.ELIG_EXCLUDED_CANON_RETIRED),
            ({"config_gate": ConfigGate.UNCLASSIFIED}, A.SCOPE, ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED),
            ({"config_gate": ConfigGate.UNDETERMINED}, A.SCOPE, ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED),
            ({"config_gate": ConfigGate.NOT_READY}, A.CONFIG, ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY),
            ({"license_gate": LegalGate.CLASS_UNCONFIRMED}, A.LEGAL_CLASS,
             ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED),
            ({"license_gate": LegalGate.LICENSE_NOT_VERIFIED}, A.LICENSE,
             ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED),
            ({"license_gate": LegalGate.LICENSE_SCOPE_MISMATCH}, A.LICENSE,
             ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH),
            ({"license_gate": LegalGate.UNDETERMINED}, A.LICENSE, ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED),
            ({"address_gate": LegalGate.LOCATION_UNKNOWN}, A.ADDRESS, ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN),
            ({"address_gate": LegalGate.ADDRESS_MISMATCH}, A.ADDRESS,
             ReasonCode.ELIG_EXCLUDED_LICENSE_ADDRESS_MISMATCH),
            ({"qualification_gate": LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED}, A.QUALIFICATION,
             ReasonCode.ELIG_EXCLUDED_QUALIFICATION_REQUIREMENT_UNCONFIRMED),
            ({"qualification_gate": LegalGate.QUALIFICATION_NOT_VERIFIED}, A.QUALIFICATION,
             ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED),
        ],
    )
    def test_one_spoiled_fact_is_one_unmet_check_with_its_own_reason(self, spoil, check, reason):
        answers = _built(**spoil)

        assert _unmet(**spoil) == [check]
        assert answers[check].reason is reason
        assert answers[check].reason in GATE_EXCLUSION_CODES | {ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE}

    def test_an_unconfirmed_class_is_one_failure_not_three(self):
        """Каталог при неподтверждённом классе отвечает «класс не подтверждён» и про лицензию, и про
        адрес: сверять их нечем. Причина одна — несошедшаяся проверка одна."""
        outcomes = _outcomes(
            license_gate=LegalGate.CLASS_UNCONFIRMED, address_gate=LegalGate.CLASS_UNCONFIRMED,
            qualification_gate=LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED,
        )

        assert outcomes[A.LEGAL_CLASS] is O.FAILED
        assert (outcomes[A.LICENSE], outcomes[A.ADDRESS]) == (O.NOT_APPLICABLE, O.NOT_APPLICABLE)

    def test_a_row_without_a_canon_fails_the_class_check_and_is_not_asked_about_qualification(self):
        outcomes = _outcomes(has_canon=False, canon_retired=None, qualification_gate=None)

        assert outcomes[A.LEGAL_CLASS] is O.FAILED, "отсутствие канона проверку не обходит"
        assert outcomes[A.QUALIFICATION] is O.NOT_APPLICABLE
        assert outcomes[A.CANON_RETIRED] is O.NOT_APPLICABLE

    def test_without_a_master_the_checks_of_the_master_do_not_apply(self):
        outcomes = _outcomes(has_master=False, address_gate=None, qualification_gate=None)

        assert (outcomes[A.ADDRESS], outcomes[A.QUALIFICATION]) == (O.NOT_APPLICABLE, O.NOT_APPLICABLE)
        assert outcomes[A.LICENSE] is O.PASSED, "проверки уровня предложения отвечают и без мастера"

    def test_not_required_is_not_applicable_and_verified_is_passed(self):
        required = _outcomes()
        not_required = _outcomes(license_verified=False, address_verified=False, qualification_verified=False)

        assert {required[c] for c in (A.LICENSE, A.ADDRESS, A.QUALIFICATION)} == {O.PASSED}
        assert {not_required[c] for c in (A.LICENSE, A.ADDRESS, A.QUALIFICATION)} == {O.NOT_APPLICABLE}

    def test_an_address_that_waits_for_the_licence_is_not_a_second_failure(self):
        outcomes = _outcomes(
            license_gate=LegalGate.LICENSE_SCOPE_MISMATCH, address_gate=LegalGate.UNDETERMINED,
            address_waits_for_license=True,
        )

        assert outcomes[A.LICENSE] is O.FAILED
        assert outcomes[A.ADDRESS] is O.NOT_APPLICABLE

    def test_two_reads_that_disagree_are_not_interpreted(self):
        """Лицензия сошлась, а адрес говорит «нет покрывающей лицензии» — это не толкуется."""
        outcomes = _outcomes(address_gate=LegalGate.UNDETERMINED, address_waits_for_license=True)

        assert outcomes[A.ADDRESS] is O.UNDETERMINED

    def test_a_legacy_row_is_judged_by_the_mapping_alone(self):
        outcomes = _outcomes(
            mapping_status=MappingStatus.UNMAPPED, canon_retired=None, config_gate=None,
            license_gate=None, address_gate=None, qualification_gate=None,
        )

        assert outcomes[A.MAPPING] is O.FAILED
        assert {outcomes[c] for c in ALL_CHECKS if c is not A.MAPPING} == {O.NOT_APPLICABLE}


class TestNotEnforcedIsNeverPassed:
    WAIVED = dict(license_verified=False, address_verified=False, qualification_verified=False)

    @pytest.mark.parametrize("check", list(UNENFORCED_LITERALS.values()))
    def test_each_unenforced_check_says_so(self, check):
        with_flag_off = _outcomes(unenforced=[check], config_gate=ConfigGate.NOT_SUBJECT, **self.WAIVED)

        assert with_flag_off[check] is O.NOT_ENFORCED
        assert O.PASSED not in {with_flag_off[check]}

    def test_positive_control_the_same_row_with_nothing_unenforced(self):
        outcomes = _outcomes(config_gate=ConfigGate.NOT_SUBJECT, **self.WAIVED)

        assert O.NOT_ENFORCED not in outcomes.values()

    def test_a_check_that_really_passed_is_not_downgraded(self):
        """Лицензия проверена — она «пройдена», что бы каталог ни говорил про флаг."""
        outcomes = _outcomes(unenforced=[A.LICENSE, A.ADDRESS, A.QUALIFICATION])

        assert {outcomes[c] for c in (A.LICENSE, A.ADDRESS, A.QUALIFICATION)} == {O.PASSED}

    def test_a_failure_is_not_softened_by_the_flag(self):
        outcomes = _outcomes(unenforced=list(UNENFORCED_LITERALS.values()), config_gate=ConfigGate.UNCLASSIFIED,
                             license_gate=LegalGate.LICENSE_NOT_VERIFIED)

        assert outcomes[A.SCOPE] is O.FAILED
        assert outcomes[A.LICENSE] is O.FAILED

    def test_the_default_fold_lets_it_through_and_the_strict_one_returns_it_as_it_is(self):
        answers = build_answers(
            mapping_status=MappingStatus.VERIFIED, canon_retired=False, config_gate=ConfigGate.NOT_SUBJECT,
            license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
            unenforced=[A.SCOPE, A.LEGAL_CLASS],
        )

        assert first_unmet(answers) is None
        strict = first_unmet(answers, not_enforced_is_unmet=True)
        assert (strict.check, strict.outcome, strict.reason) == (A.SCOPE, O.NOT_ENFORCED, None)

    def test_the_strict_fold_still_puts_a_real_failure_first_in_order(self):
        answers = build_answers(
            mapping_status=MappingStatus.UNMAPPED, canon_retired=False, config_gate=ConfigGate.NOT_SUBJECT,
            license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
            unenforced=[A.SCOPE],
        )

        assert first_unmet(answers, not_enforced_is_unmet=True).check is A.MAPPING


class TestTheFold:
    def test_the_first_unmet_in_the_order_of_the_list(self):
        answers = build_answers(
            mapping_status=MappingStatus.VERIFIED, canon_retired=True, config_gate=ConfigGate.NOT_READY,
            license_gate=LegalGate.LICENSE_NOT_VERIFIED, address_gate=LegalGate.CLEARED,
            qualification_gate=LegalGate.CLEARED,
        )

        assert first_unmet(answers).check is A.CANON_RETIRED

    def test_a_disabled_check_does_not_exclude_and_the_next_one_speaks(self):
        answers = build_answers(
            mapping_status=MappingStatus.VERIFIED, canon_retired=True, config_gate=ConfigGate.NOT_READY,
            license_gate=LegalGate.CLEARED, address_gate=LegalGate.CLEARED, qualification_gate=LegalGate.CLEARED,
        )

        without_retired = [c for c in ALL_CHECKS if c is not A.CANON_RETIRED]
        assert first_unmet(answers, enabled=without_retired).check is A.CONFIG
        assert first_unmet(answers, enabled=[]) is None
        assert first_unmet(answers, enabled=[A.LICENSE]) is None


# -- S1 отвечает теми же кодами, что цепочка условий ---------------------------

#: Что отвечала цепочка условий в S1 до DRF-2888 — записано отдельно от кода,
#: который её заменил.
_CONFIG_CODE = {
    ConfigGate.UNCLASSIFIED: ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED,
    ConfigGate.NOT_READY: ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY,
    ConfigGate.UNDETERMINED: ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED,
}
_LEGAL_CODE = {
    LegalGate.CLASS_UNCONFIRMED: ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED,
    LegalGate.LICENSE_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_MEDICAL_LICENSE_NOT_VERIFIED,
    LegalGate.LICENSE_SCOPE_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_SCOPE_MISMATCH,
    LegalGate.LOCATION_UNKNOWN: ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN,
    LegalGate.ADDRESS_MISMATCH: ReasonCode.ELIG_EXCLUDED_LICENSE_ADDRESS_MISMATCH,
    LegalGate.QUALIFICATION_REQUIREMENT_UNCONFIRMED: ReasonCode.ELIG_EXCLUDED_QUALIFICATION_REQUIREMENT_UNCONFIRMED,
    LegalGate.QUALIFICATION_NOT_VERIFIED: ReasonCode.ELIG_EXCLUDED_PRACTITIONER_QUALIFICATION_NOT_VERIFIED,
    LegalGate.UNDETERMINED: ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED,
}


def _chain_of_ifs(mapping, retired, config, legal) -> ReasonCode | None:
    if mapping is not MappingStatus.VERIFIED:
        return ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE
    if retired is True:
        return ReasonCode.ELIG_EXCLUDED_CANON_RETIRED
    if config is not None and config not in (ConfigGate.NOT_SUBJECT, ConfigGate.READY):
        return _CONFIG_CODE[config]
    if legal is not None and legal is not LegalGate.CLEARED:
        return _LEGAL_CODE[legal]
    return None


_EVERY_COMBINATION = list(itertools.product(
    list(MappingStatus), [None, False, True], [None, *ConfigGate], [None, *LegalGate],
))


def test_the_grid_is_not_empty_and_covers_every_value():
    assert len(_EVERY_COMBINATION) == len(MappingStatus) * 3 * (len(ConfigGate) + 1) * (len(LegalGate) + 1)
    assert len(_EVERY_COMBINATION) > 500


@pytest.mark.parametrize(("mapping", "retired", "config", "legal"), _EVERY_COMBINATION)
def test_s1_answers_exactly_as_the_chain_of_conditions_did(mapping, retired, config, legal):
    candidate = make_facts(mapping_status=mapping, canon_retired=retired, config_gate=config, legal_gate=legal)

    result = apply_eligibility([candidate], make_request(), StagePolicy())

    expected = _chain_of_ifs(mapping, retired, config, legal)
    got = result.excluded[0].reason_code if result.excluded else None
    assert got is expected
    assert bool(result.admitted) is (expected is None)


@pytest.mark.parametrize(("mapping", "retired", "config", "legal"), _EVERY_COMBINATION)
def test_the_collapsed_gates_always_give_eight_answers_in_order(mapping, retired, config, legal):
    answers = answers_from_collapsed_gates(
        mapping_status=mapping, canon_retired=retired, config_gate=config, legal_gate=legal,
    )

    assert [a.check for a in answers] == list(ALL_CHECKS)


class TestASubsetOfChecksIsADiagnosticPolicy:
    def _admit(self, facts, checks):
        return apply_eligibility([facts], make_request(), StagePolicy(admission_checks=frozenset(checks)))

    def test_the_default_policy_applies_every_check(self):
        assert StagePolicy().admission_checks == frozenset(ALL_CHECKS)
        assert StagePolicy.from_settings().admission_checks == frozenset(ALL_CHECKS)

    def test_with_no_checks_an_unmapped_candidate_passes_but_is_not_called_verified(self):
        candidate = make_facts(mapping_status=MappingStatus.UNMAPPED)

        result = self._admit(candidate, [])

        assert [f.ref.id for f in result.admitted] == [candidate.ref.id]
        assert ReasonCode.ELIG_CAPABILITY_VERIFIED not in result.codes[candidate.ref.id]
        assert result.codes[candidate.ref.id], "без объяснения кандидат в выдачу не попадает"

    def test_positive_control_with_every_check_the_same_candidate_is_excluded(self):
        candidate = make_facts(mapping_status=MappingStatus.UNMAPPED)

        result = self._admit(candidate, ALL_CHECKS)

        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE]

    def test_a_verified_candidate_keeps_the_evidence_when_the_mapping_is_checked(self):
        candidate = make_facts()

        result = self._admit(candidate, ALL_CHECKS)

        assert ReasonCode.ELIG_CAPABILITY_VERIFIED in result.codes[candidate.ref.id]

    def test_a_verified_candidate_is_not_called_verified_when_the_mapping_was_not_checked(self):
        """Связь у кандидата подтверждена, но прогон её не проверял — свидетельства в отчёте нет."""
        candidate = make_facts()

        result = self._admit(candidate, [A.CONFIG])

        assert [f.ref.id for f in result.admitted] == [candidate.ref.id]
        assert ReasonCode.ELIG_CAPABILITY_VERIFIED not in result.codes[candidate.ref.id]

    def test_a_source_that_collapsed_its_answers_cannot_vouch_for_the_checks_after_the_failure(self):
        """Источник отдал только «лицензия не сошлась». Что с квалификацией — он не сказал, и
        диагностический прогон «только квалификация» не вправе считать её пройденной."""
        candidate = make_facts(legal_gate=LegalGate.LICENSE_NOT_VERIFIED)

        alone = self._admit(candidate, [A.QUALIFICATION])
        before = self._admit(candidate, [A.LEGAL_CLASS])

        assert [e.reason_code for e in alone.excluded] == [ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED]
        assert before.excluded == (), "а про проверку ДО несошедшейся он сказал: она прошла"

    def test_one_check_alone_excludes_only_by_itself(self):
        candidate = make_facts(
            mapping_status=MappingStatus.UNMAPPED, canon_retired=True, config_gate=ConfigGate.NOT_READY,
        )

        assert self._admit(candidate, [A.CONFIG]).excluded[0].reason_code is ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY
        assert self._admit(candidate, [A.CANON_RETIRED]).excluded[0].reason_code is (
            ReasonCode.ELIG_EXCLUDED_CANON_RETIRED
        )
        assert self._admit(candidate, [A.LICENSE]).excluded == ()

    def test_switching_the_checks_off_does_not_switch_safety_off(self):
        """Диагностика отключает допуск каталога — не безопасность хода."""
        from recommendation._types import SafetyState

        candidate = make_facts()
        blocked = apply_eligibility(
            [candidate], make_request(safety_state=SafetyState.STOP), StagePolicy(admission_checks=frozenset()),
        )

        assert [e.reason_code for e in blocked.excluded] == [ReasonCode.ELIG_EXCLUDED_SAFETY]
