"""O-1 — предпочтения клиента в резолвере (DRF-2816, решение владельца 06.10, handoff §3 / §11).

Что заперто:

- мягкое «предпочитаю X» поднимает X на S4; жёсткое «только X» исключает не-X на S1;
- жёсткое работает только на ДОПУСТИМЫХ: проверки процедуры и связи выше него;
- соответствие запросу (S2) выше предпочтения (S4): «мой мастер» не обгоняет
  более точное совпадение с тем, о чём спросили сейчас;
- текущий запрос > память — в одном измерении; разные измерения работают вместе;
- «только X» из памяти — мягкое; вывод агента не участвует вовсе;
- предпочтение не делает выдачу «персонализированной прошлым опытом» (§72):
  SafetyResult для него не нужен (O-1);
- пустая выдача из-за «только X» объясняет себя кодом решения.
"""
from __future__ import annotations

import uuid
from unittest import mock

import pytest

from recommendation import _serializers as serializers_module
from recommendation._pipeline import resolve
from recommendation._reason_codes import ReasonCode
from recommendation._serializers import MAX_PREFERENCES, ResolveRequestSerializer, build_preferences
from recommendation._types import (
    MappingStatus,
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    Preference,
    PreferenceKind,
    PreferenceOrigin,
    PreferenceStrength,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    StageId,
    Surface,
)
from recommendation.tests.conftest import make_facts

NOW, MEMORY = PreferenceOrigin.CURRENT_REQUEST, PreferenceOrigin.CONFIRMED_MEMORY
SOFT, HARD = PreferenceStrength.SOFT, PreferenceStrength.HARD


class _Fixed:
    def __init__(self, facts):
        self._facts = facts

    def fetch(self, *, scope, need):  # noqa: ARG002 — порт шире заглушки
        return list(self._facts)


def _resolve(facts, *prefs, need=None):
    return resolve(RecommendationRequest(
        request_id="r-o1", subject_ref="s", surface=Surface.MINIAPP_HOME, scope=Scope(ScopeMode.MARKETPLACE),
        need=need or NeedSpec(origin=NeedOrigin.MEMORY), safety_state=SafetyState.NOT_APPLICABLE,
        tie_break_seed="seed-o1", k=10, preferences=tuple(prefs),
    ), source=_Fixed(facts))


def _master(cid, strength=SOFT, origin=NOW):
    return Preference(kind=PreferenceKind.MASTER, ref=cid, strength=strength, origin=origin)


def _tiers(decision):
    return {c.candidate_ref.id: c.tier for c in decision.ordered}


def _codes(decision, cid):
    return next(set(c.reason_codes) for c in decision.ordered if c.candidate_ref.id == cid)


class TestSoftAndHard:
    def test_a_soft_preference_lifts_the_preferred_master(self):
        anna, other = make_facts(), make_facts()

        decision = _resolve([anna, other], _master(anna.ref.id))

        tiers = _tiers(decision)
        assert tiers[anna.ref.id] < tiers[other.ref.id]
        assert decision.separation_stage is StageId.S4
        assert ReasonCode.CONTEXT_PREFERENCE_CURRENT_REQUEST in _codes(decision, anna.ref.id)

    def test_a_hard_preference_excludes_everyone_else(self):
        anna, other = make_facts(), make_facts()

        decision = _resolve([anna, other], _master(anna.ref.id, HARD))

        assert [c.candidate_ref.id for c in decision.ordered] == [anna.ref.id]
        assert [(e.candidate_ref.id, e.reason_code) for e in decision.excluded] == [
            (other.ref.id, ReasonCode.ELIG_EXCLUDED_PREFERENCE_HARD),
        ]

    def test_a_hard_preference_applies_only_to_admissible_candidates(self):
        """«Только Анна», а связь Анны не проверена — Анна не допускается предпочтением."""
        anna = make_facts(mapping_status=MappingStatus.REVIEW_REQUIRED)

        decision = _resolve([anna], _master(anna.ref.id, HARD))

        assert decision.ordered == ()
        assert [e.reason_code for e in decision.excluded] == [ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE]

    def test_an_empty_answer_from_only_x_names_it(self):
        other = make_facts()

        decision = _resolve([other], _master(uuid.uuid4(), HARD))

        assert decision.ordered == ()
        assert ReasonCode.ELIG_EXCLUDED_PREFERENCE_HARD in decision.reason_codes

    def test_the_need_still_outranks_the_preference(self):
        """S2 выше S4: точное совпадение с тем, о чём спросили, выше «моего мастера» с частичным."""
        exact = make_facts(match_level=MatchLevel.SERVICE_EXACT)
        anna_partial = make_facts(match_level=MatchLevel.SERVICE_PARTIAL)

        decision = _resolve(
            [exact, anna_partial], _master(anna_partial.ref.id),
            need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="массаж"),
        )

        tiers = _tiers(decision)
        assert tiers[exact.ref.id] < tiers[anna_partial.ref.id]
        assert decision.separation_stage is StageId.S2


class TestCurrentOverHistory:
    def test_in_the_same_dimension_the_current_request_wins(self):
        remembered, named_now = make_facts(), make_facts()

        decision = _resolve(
            [remembered, named_now],
            _master(remembered.ref.id, origin=MEMORY), _master(named_now.ref.id, origin=NOW),
        )

        tiers = _tiers(decision)
        assert tiers[named_now.ref.id] < tiers[remembered.ref.id]
        assert ReasonCode.CONTEXT_PREFERENCE_CONFIRMED_MEMORY not in _codes(decision, remembered.ref.id), (
            "память того же вида, что назван сейчас, не учитывается"
        )

    def test_different_dimensions_both_work(self):
        """Назван мастер сейчас; категория — из памяти. Категория работает."""
        massage, manicure = uuid.uuid4(), uuid.uuid4()
        # Категории известны у всех: неизвестные молчат (§29.4), это отдельный узел.
        named_now = make_facts(category_refs=frozenset({manicure}))
        in_remembered_category = make_facts(category_refs=frozenset({massage}))
        neither = make_facts(category_refs=frozenset({manicure}))

        decision = _resolve(
            [named_now, in_remembered_category, neither],
            _master(named_now.ref.id, origin=NOW),
            Preference(kind=PreferenceKind.CATEGORY, ref=massage, origin=MEMORY),
        )

        tiers = _tiers(decision)
        assert tiers[named_now.ref.id] < tiers[in_remembered_category.ref.id] < tiers[neither.ref.id]
        assert ReasonCode.CONTEXT_PREFERENCE_CONFIRMED_MEMORY in _codes(decision, in_remembered_category.ref.id)

    def test_only_x_from_memory_is_read_as_soft(self):
        anna, other = make_facts(), make_facts()

        decision = _resolve([anna, other], _master(anna.ref.id, HARD, origin=MEMORY))

        assert {c.candidate_ref.id for c in decision.ordered} == {anna.ref.id, other.ref.id}, (
            "устаревшее «только» из памяти не прячет остальных"
        )
        assert _tiers(decision)[anna.ref.id] < _tiers(decision)[other.ref.id]


class TestWhatDoesNotParticipate:
    def test_an_agent_inference_is_dropped(self):
        items = [
            {"kind": "master", "ref": uuid.uuid4(), "strength": "soft", "origin": "agent_inference"},
            {"kind": "salon", "ref": uuid.uuid4(), "strength": "soft", "origin": "current_request"},
        ]

        with mock.patch.object(serializers_module.logger, "warning") as warning:
            prefs = build_preferences(items)

        assert [p.kind for p in prefs] == [PreferenceKind.SALON]
        assert warning.call_count == 1
        assert "agent_inference" in warning.call_args.args

    def test_the_wire_accepts_preferences_and_caps_their_number(self):
        base = {
            "request_id": "r", "surface": "MINIAPP_HOME", "scope": {"mode": "MARKETPLACE"},
            "need": {"origin": "MEMORY"}, "safety_state": "NOT_APPLICABLE",
        }
        one = {"kind": "master", "ref": str(uuid.uuid4()), "origin": "current_request"}

        ok = ResolveRequestSerializer(data={**base, "preferences": [one]})
        too_many = ResolveRequestSerializer(data={**base, "preferences": [one] * (MAX_PREFERENCES + 1)})

        assert ok.is_valid(), ok.errors
        assert ok.build_preferences()[0].strength is SOFT, "сила по умолчанию — мягкая"
        assert not too_many.is_valid()

    def test_a_preference_does_not_make_the_answer_personalised_by_past_experience(self):
        """§72: SafetyResult не нужен для предпочтения (O-1) — список не закрывается целиком."""
        anna, other = make_facts(), make_facts()

        decision = _resolve([anna, other], _master(anna.ref.id))

        assert len(decision.ordered) == 2
        assert not any(e.reason_code is ReasonCode.ELIG_EXCLUDED_SAFETY for e in decision.excluded)

    def test_no_preferences_keeps_todays_answer(self):
        a, b = make_facts(), make_facts()

        decision = _resolve([a, b])

        assert len({c.tier for c in decision.ordered}) == 1, "без предпочтений S4 молчит, как до O-1"


@pytest.mark.parametrize("kind_attr", ["salon", "category"])
def test_salon_and_category_preferences_match_their_dimension(kind_attr):
    target = uuid.uuid4()
    hit = make_facts(tenant_ref=target) if kind_attr == "salon" else make_facts(category_refs=frozenset({target}))
    miss = make_facts(category_refs=frozenset({uuid.uuid4()}))

    decision = _resolve([hit, miss], Preference(kind=PreferenceKind(kind_attr), ref=target, origin=NOW))

    assert _tiers(decision)[hit.ref.id] < _tiers(decision)[miss.ref.id]


class TestReviewFixesV2:
    """Находки ревью O-1 (DRF-2816 v2): каждая — узлом, красным на e158d2d8."""

    def test_two_hard_masters_mean_either_of_them(self):
        """«Анна или Мария»: внутри вида — ИЛИ, а не «и та, и другая сразу» (никто)."""
        anna, maria, other = make_facts(), make_facts(), make_facts()

        decision = _resolve([anna, maria, other], _master(anna.ref.id, HARD), _master(maria.ref.id, HARD))

        assert {c.candidate_ref.id for c in decision.ordered} == {anna.ref.id, maria.ref.id}
        assert [e.candidate_ref.id for e in decision.excluded] == [other.ref.id]

    def test_hard_preferences_of_different_kinds_all_apply(self):
        """Между видами — И: «Анна или Мария, и только в этом салоне»."""
        salon = uuid.uuid4()
        anna_here, maria_elsewhere = make_facts(tenant_ref=salon), make_facts()

        decision = _resolve(
            [anna_here, maria_elsewhere],
            _master(anna_here.ref.id, HARD), _master(maria_elsewhere.ref.id, HARD),
            Preference(kind=PreferenceKind.SALON, ref=salon, strength=HARD, origin=NOW),
        )

        assert [c.candidate_ref.id for c in decision.ordered] == [anna_here.ref.id]

    def test_with_a_stated_need_category_is_read_from_the_matched_service(self):
        """Спросили маникюр; «лучше там, где массаж» — мастер, совпавший маникюром, массажем не отвечает."""
        massage, manicure = uuid.uuid4(), uuid.uuid4()
        also_massage = make_facts(
            match_level=MatchLevel.SERVICE_PARTIAL, category_refs=frozenset({massage, manicure}),
            matched_category_refs=frozenset({manicure}),
        )
        matched_by_massage = make_facts(
            match_level=MatchLevel.SERVICE_PARTIAL, category_refs=frozenset({massage}),
            matched_category_refs=frozenset({massage}),
        )

        decision = _resolve(
            [also_massage, matched_by_massage],
            Preference(kind=PreferenceKind.CATEGORY, ref=massage, origin=NOW),
            need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="маникюр"),
        )

        tiers = _tiers(decision)
        assert tiers[matched_by_massage.ref.id] < tiers[also_massage.ref.id]

    def test_one_stronger_match_outweighs_two_weaker_ones(self):
        """Не сумма: мастер (старшее измерение) выше салона и категории вместе."""
        salon, category = uuid.uuid4(), uuid.uuid4()
        named_master = make_facts(category_refs=frozenset({uuid.uuid4()}))
        salon_and_category = make_facts(tenant_ref=salon, category_refs=frozenset({category}))

        decision = _resolve(
            [salon_and_category, named_master],
            _master(named_master.ref.id),
            Preference(kind=PreferenceKind.SALON, ref=salon, origin=NOW),
            Preference(kind=PreferenceKind.CATEGORY, ref=category, origin=NOW),
        )

        tiers = _tiers(decision)
        assert tiers[named_master.ref.id] < tiers[salon_and_category.ref.id]

    def test_unknown_categories_are_not_demoted(self):
        """§29.4: категорий не знаем — предпочтение категории молчит, а не ставит ниже."""
        massage = uuid.uuid4()
        in_category = make_facts(category_refs=frozenset({massage}))
        unknown = make_facts()

        decision = _resolve(
            [in_category, unknown], Preference(kind=PreferenceKind.CATEGORY, ref=massage, origin=NOW),
        )

        tiers = _tiers(decision)
        assert tiers[in_category.ref.id] == tiers[unknown.ref.id]

    def test_the_cap_holds_in_process_too(self):
        too_many = tuple(_master(uuid.uuid4()) for _ in range(MAX_PREFERENCES + 1))

        with pytest.raises(ValueError, match="предпочтений"):
            RecommendationRequest(
                request_id="r", subject_ref="s", surface=Surface.MINIAPP_HOME,
                scope=Scope(ScopeMode.MARKETPLACE), need=NeedSpec(origin=NeedOrigin.MEMORY),
                preferences=too_many,
            )

    def test_the_wire_accepts_null_preferences(self):
        data = {
            "request_id": "r", "surface": "MINIAPP_HOME", "scope": {"mode": "MARKETPLACE"},
            "need": {"origin": "MEMORY"}, "safety_state": "NOT_APPLICABLE", "preferences": None,
        }

        serializer = ResolveRequestSerializer(data=data)

        assert serializer.is_valid(), serializer.errors
        assert serializer.build_preferences() == ()
