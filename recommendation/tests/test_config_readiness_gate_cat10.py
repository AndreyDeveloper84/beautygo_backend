"""CAT-10 (чинит C1) — резолверная половина гейта готовности конфигурации body-care.

Источниковая половина (пакетное чтение CAT-6 и заполнение ``config_ready``) —
после слияния CAT-6; здесь — то, что не зависит от имени её функции:

- ``config_ready=False`` (body-care, CAT-6 не READY) → исключён S1 с
  ``ELIG_EXCLUDED_CONFIG_NOT_READY``, даже при VERIFIED-связи;
- ``config_ready=True`` (вне Body Care или READY) → допущен;
- ``config_ready=None`` (источник не сообщает — все источники до CAT-6) → допущен,
  гейт не применяется: каталог вне Body Care не закрывается;
- связь не VERIFIED → прежний код NOT_RECOMMENDABLE (связь проверяется первой,
  коды не смешиваются);
- пустая полка из-за неготовности называет это в кодах решения;
- код — в реестре исключений.
"""
from __future__ import annotations

import uuid

import pytest

from recommendation._pipeline import resolve
from recommendation._reason_codes import EXCLUSION_CODES, ReasonCode
from recommendation._stages import StagePolicy, apply_eligibility
from recommendation._types import (
    MappingStatus,
    NeedOrigin,
    NeedSpec,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)
from recommendation.tests.conftest import make_facts


def _request(**need) -> RecommendationRequest:
    return RecommendationRequest(
        request_id="r-cat10", subject_ref="s", surface=Surface.MINIAPP_HOME, scope=Scope(ScopeMode.MARKETPLACE),
        need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT, **need) if need else NeedSpec(origin=NeedOrigin.MEMORY),
        safety_state=SafetyState.NOT_APPLICABLE, tie_break_seed="s", k=5,
    )


def _admit(*facts):
    return apply_eligibility(list(facts), _request(), StagePolicy())


class _Fixed:
    def __init__(self, facts):
        self._facts = facts

    def fetch(self, *, scope, need):  # noqa: ARG002 — порт шире заглушки
        return list(self._facts)


class TestTheGate:
    def test_a_verified_but_unready_body_care_offer_is_excluded(self):
        unready = make_facts(config_ready=False)

        result = _admit(unready)

        assert result.admitted == ()
        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY]

    def test_a_ready_offer_is_admitted(self):
        ready = make_facts(config_ready=True)

        result = _admit(ready)

        assert [f.ref.id for f in result.admitted] == [ready.ref.id]

    def test_a_source_that_says_nothing_does_not_close_the_catalog(self):
        """Положительный контроль на весь каталог вне Body Care: ``None`` — не закрытие."""
        silent = make_facts(config_ready=None)

        result = _admit(silent)

        assert [f.ref.id for f in result.admitted] == [silent.ref.id]

    def test_the_mapping_is_checked_first_and_codes_do_not_mix(self):
        unmapped_and_unready = make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED, config_ready=False)

        result = _admit(unmapped_and_unready)

        assert [e.reason_code for e in result.excluded] == [ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE]

    def test_neighbours_are_unaffected(self):
        unready = make_facts(config_ready=False)
        other = make_facts(config_ready=None)

        result = _admit(unready, other)

        assert [f.ref.id for f in result.admitted] == [other.ref.id]


class TestTheDecisionExplainsItself:
    def test_an_empty_shelf_from_unreadiness_names_it(self):
        decision = resolve(_request(), source=_Fixed([make_facts(config_ready=False), make_facts(config_ready=False)]))

        assert decision.ordered == ()
        assert ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY in decision.reason_codes

    def test_a_non_empty_shelf_does_not_carry_the_summary_code(self):
        decision = resolve(_request(), source=_Fixed([make_facts(config_ready=False), make_facts(config_ready=True)]))

        assert len(decision.ordered) == 1
        assert ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY not in decision.reason_codes


def test_the_code_is_an_exclusion_code():
    assert ReasonCode.ELIG_EXCLUDED_CONFIG_NOT_READY in EXCLUSION_CODES


@pytest.mark.parametrize("ready", [True, None])
def test_ready_or_silent_candidates_keep_the_verified_evidence(ready):
    candidate = make_facts(config_ready=ready, cid=uuid.uuid4())

    result = _admit(candidate)

    assert ReasonCode.ELIG_CAPABILITY_VERIFIED in result.codes[candidate.ref.id]
