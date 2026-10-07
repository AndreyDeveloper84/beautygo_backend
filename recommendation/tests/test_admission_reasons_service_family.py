"""Раздельные причины недопуска каталога — резолверная половина фикса ``service_family``.

Решение владельца 07.10: классификация правдива, допуск считается отдельно
всеми применимыми проверками, причины не сливаются. Что заперто здесь:

- «область канона неизвестна» (``unclassified`` от CAT-6) закрывает своим
  кодом — и источник узнаёт этот литерал раньше, чем его начнут отдавать;
- «не классифицировано», «не готово» и «не смогли прочитать» — три разных
  кода; дефект чтения не выглядит незаполненными данными;
- у каждого значения обоих гейтов свой код, общего «не допущен» нет;
- неклассифицированная строка при выборе строки стоит как закрытая;
- маркер каталога — не состояние §7: шов не примет его за «не готово».
"""
from __future__ import annotations

import logging
import uuid

import pytest

from recommendation._pipeline import resolve
from recommendation._reason_codes import EXCLUSION_CODES, GATE_EXCLUSION_CODES, REGISTRY_VERSION, ReasonCode
from recommendation._stages import StagePolicy, apply_eligibility
from recommendation._types import (
    ConfigGate,
    LegalGate,
    MappingStatus,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)
from recommendation.tests.conftest import StaticSource, make_facts
from services import body_care_validation
from users import recommendation_source
from users.recommendation_source import UNCLASSIFIED, _MappingFacts, config_readiness


def _request() -> RecommendationRequest:
    return RecommendationRequest(
        request_id="r-reasons", subject_ref="s", surface=Surface.MINIAPP_HOME, scope=Scope(ScopeMode.MARKETPLACE),
        need=NeedSpec(origin=NeedOrigin.MEMORY), safety_state=SafetyState.NOT_APPLICABLE, tie_break_seed="s", k=5,
    )


def _admit(*facts):
    return apply_eligibility(list(facts), _request(), StagePolicy())


def _code(**facts) -> ReasonCode:
    result = _admit(make_facts(**facts))
    assert result.admitted == ()
    [exclusion] = result.excluded
    return exclusion.reason_code


@pytest.fixture
def source_log(caplog):
    """У ``users`` в настройках ``propagate=False`` — перехватчик вешается на сам логгер."""
    source_logger = logging.getLogger(recommendation_source.__name__)
    source_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        source_logger.removeHandler(caplog.handler)


def _errors(log) -> list[str]:
    return [r.getMessage() for r in log.records if r.levelno >= logging.ERROR]


class TestTheSeamKnowsTheMarkerBeforeItIsProduced:
    def test_unclassified_becomes_its_own_value_and_is_not_an_error(self, monkeypatch, source_log):
        pk = uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "validation_states", lambda ids: {pk: "unclassified"})

        assert config_readiness([pk]) == {pk: ConfigGate.UNCLASSIFIED}
        assert _errors(source_log) == [], "состояние данных, не дефект чтения: лог не заливается"

    def test_the_literal_is_the_one_agreed_with_the_catalog(self):
        assert UNCLASSIFIED == "unclassified"

    def test_positive_control_an_unknown_literal_is_still_a_reading_defect(self, monkeypatch, source_log):
        pk = uuid.uuid4()
        monkeypatch.setattr(recommendation_source, "validation_states", lambda ids: {pk: "unclasified"})

        assert config_readiness([pk]) == {pk: ConfigGate.UNDETERMINED}
        assert len(_errors(source_log)) == 1

    def test_every_answer_the_catalog_gives_today_has_a_value(self, monkeypatch):
        """По всем ответам функции: ни один сегодняшний ответ CAT-6 не уходит в «не определено»."""
        answers = (body_care_validation.NOT_SUBJECT, *body_care_validation.VALIDATION_STATES, UNCLASSIFIED)
        pks = {answer: uuid.uuid4() for answer in answers}
        monkeypatch.setattr(
            recommendation_source, "validation_states", lambda ids: {pk: answer for answer, pk in pks.items()},
        )

        gates = config_readiness(pks.values())

        assert ConfigGate.UNDETERMINED not in gates.values()
        assert {answer for answer, pk in pks.items() if gates[pk] in (ConfigGate.NOT_SUBJECT, ConfigGate.READY)} == {
            body_care_validation.NOT_SUBJECT, body_care_validation.READY_FOR_SCREENING,
        }

    def test_the_marker_is_not_a_state_of_the_ladder(self):
        """Маркер, как и ``not_subject``, — не состояние §7: шов разбирает его
        раньше набора состояний и не примет за «не готово»."""
        assert UNCLASSIFIED not in (body_care_validation.NOT_SUBJECT, *body_care_validation.VALIDATION_STATES)


class TestEveryReasonHasItsOwnCode:
    def test_the_three_closing_values_of_the_config_gate_differ(self):
        closing = [gate for gate in ConfigGate if gate not in (ConfigGate.NOT_SUBJECT, ConfigGate.READY)]
        codes = {gate: _code(config_gate=gate) for gate in closing}

        assert codes == {
            ConfigGate.UNCLASSIFIED: ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED,
            ConfigGate.NOT_READY: ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY,
            ConfigGate.UNDETERMINED: ReasonCode.ELIG_EXCLUDED_ELIGIBILITY_UNDETERMINED,
        }

    @pytest.mark.parametrize("gate", [ConfigGate.NOT_SUBJECT, ConfigGate.READY, None])
    def test_only_two_values_and_silence_open(self, gate):
        candidate = make_facts(config_gate=gate)

        assert [f.ref.id for f in _admit(candidate).admitted] == [candidate.ref.id]

    def test_unclassified_is_not_class_unconfirmed(self):
        """П2 ≠ П4: «область неизвестна» и «юр. класс не подтверждён» — разные причины."""
        unclassified = _code(config_gate=ConfigGate.UNCLASSIFIED)
        class_unconfirmed = _code(legal_gate=LegalGate.CLASS_UNCONFIRMED)

        assert unclassified is ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED
        assert class_unconfirmed is ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED

    def test_no_two_data_reasons_share_a_code(self):
        """По обоим перечислениям: общий код есть только у дефекта чтения — он один и тот же дефект."""
        config = {g: _code(config_gate=g) for g in ConfigGate if g not in (ConfigGate.NOT_SUBJECT, ConfigGate.READY)}
        legal = {g: _code(legal_gate=g) for g in LegalGate if g is not LegalGate.CLEARED}
        data_reasons = [
            code for gate, code in (*config.items(), *legal.items())
            if gate not in (ConfigGate.UNDETERMINED, LegalGate.UNDETERMINED)
        ]

        assert len(data_reasons) == len(set(data_reasons)) == 9
        assert config[ConfigGate.UNDETERMINED] is legal[LegalGate.UNDETERMINED]
        assert config[ConfigGate.UNDETERMINED] not in data_reasons

    def test_the_old_catch_all_is_never_issued_but_still_readable(self):
        """Код 1.4.0 остаётся в реестре ради записанных под ним решений, но ни одно значение
        ни одного гейта к нему больше не ведёт."""
        issued = {_code(config_gate=g) for g in ConfigGate if g not in (ConfigGate.NOT_SUBJECT, ConfigGate.READY)}
        issued |= {_code(legal_gate=g) for g in LegalGate if g is not LegalGate.CLEARED}

        assert ReasonCode.ELIG_EXCLUDED_LEGAL_NOT_CONFIRMED not in issued
        assert ReasonCode.ELIG_EXCLUDED_LEGAL_NOT_CONFIRMED not in GATE_EXCLUSION_CODES
        assert ReasonCode.ELIG_EXCLUDED_LEGAL_NOT_CONFIRMED in EXCLUSION_CODES
        assert issued <= GATE_EXCLUSION_CODES
        assert not [code for code in ReasonCode if code.value.endswith("NOT_ELIGIBLE")]

    def test_the_gate_codes_are_registered_exclusions(self):
        assert GATE_EXCLUSION_CODES <= EXCLUSION_CODES
        assert len(GATE_EXCLUSION_CODES) == 10
        assert tuple(int(p) for p in REGISTRY_VERSION.split(".")[:2]) >= (1, 5)


class TestTheOrderOfChecks:
    def test_the_mapping_comes_before_the_scope(self):
        code = _code(mapping_status=MappingStatus.REVIEW_REQUIRED, config_gate=ConfigGate.UNCLASSIFIED)

        assert code is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE

    def test_the_scope_comes_before_the_legal_class(self):
        """На пилоте после включения у строки не сойдутся обе: наружу идёт первая — П2."""
        code = _code(config_gate=ConfigGate.UNCLASSIFIED, legal_gate=LegalGate.CLASS_UNCONFIRMED)

        assert code is ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED

    def test_an_empty_shelf_names_every_reason_that_closed_it(self):
        decision = resolve(_request(), source=StaticSource([
            make_facts(config_gate=ConfigGate.UNCLASSIFIED),
            make_facts(legal_gate=LegalGate.CLASS_UNCONFIRMED),
            make_facts(legal_gate=LegalGate.LOCATION_UNKNOWN),
        ]))

        assert decision.ordered == ()
        assert {
            ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED,
            ReasonCode.ELIG_EXCLUDED_LEGAL_CLASS_UNCONFIRMED,
            ReasonCode.ELIG_EXCLUDED_MASTER_LOCATION_UNKNOWN,
        } <= set(decision.reason_codes)

    def test_a_shelf_that_is_not_empty_carries_no_summary_of_the_closed_ones(self):
        decision = resolve(_request(), source=StaticSource([
            make_facts(config_gate=ConfigGate.UNCLASSIFIED), make_facts(config_gate=ConfigGate.NOT_SUBJECT),
        ]))

        assert len(decision.ordered) == 1
        assert ReasonCode.ELIG_EXCLUDED_CATALOG_UNCLASSIFIED not in decision.reason_codes


class TestAnUnclassifiedRowRanksAsClosed:
    def _facts(self, rows) -> _MappingFacts:
        mapping = _MappingFacts()
        mapping.has_service = True
        for pk, config in rows.items():
            mapping.status_by_service[pk] = "verified"
            mapping.config_by_service[pk] = config
            mapping.legal_by_service[pk] = LegalGate.CLEARED
        return mapping

    @pytest.mark.parametrize("closed", [ConfigGate.UNCLASSIFIED, ConfigGate.NOT_READY, ConfigGate.UNDETERMINED])
    @pytest.mark.parametrize("opened", [ConfigGate.NOT_SUBJECT, ConfigGate.READY])
    def test_an_open_row_answers_for_the_master_whatever_the_order(self, closed, opened):
        first, second = uuid.uuid4(), uuid.uuid4()

        assert self._facts({first: closed, second: opened}).config_gate() is opened
        assert self._facts({first: opened, second: closed}).config_gate() is opened

    def test_only_unclassified_rows_close_the_master_with_that_reason(self):
        mapping = self._facts({uuid.uuid4(): ConfigGate.UNCLASSIFIED, uuid.uuid4(): ConfigGate.UNCLASSIFIED})

        assert mapping.config_gate() is ConfigGate.UNCLASSIFIED
