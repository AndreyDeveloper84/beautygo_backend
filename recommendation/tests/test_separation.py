"""H1-в: стадия, которая первой разделила лучший ярус (DRF-1934).

Решение владельца 15.09 (`docs/OWNER_QUESTIONS_2026-09-12.md` §H1): «в, считает
каталог». Решения главного окна по замеру
`docs/MEASURE_DRF1934_SEPARATION_2026-09-15.md`:

* Р1 — стадия кодом, число в [0,1] каталог не выдумывает, `separation` = null;
* Р2 — рядом размер лучшего яруса, порог решает мозг;
* Р3 — отсутствие стадии называется явным состоянием, а не угадывается.

Кандидаты собраны теми же фабриками, что в `test_pipeline`: стадия, названная
здесь, должна быть той же, что там ставит вердикты.
"""
from __future__ import annotations

import pytest

from recommendation._serializers import ResolveResponseSerializer, decision_to_payload
from recommendation.api import (
    RESOLVER_SPEC_VERSION,
    Constraint,
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    ScheduleState,
    SeparationState,
    StageId,
    StagePolicy,
    StageVerdict,
    UserConstraints,
    resolve,
)

from .conftest import StaticSource, make_facts, make_request

_QUALITY = StagePolicy(n_substantiated=10)
_K5_REQUEST_KW = {"constraints": UserConstraints(time_window=Constraint.known("today"))}
_K5_POLICY = StagePolicy(availability_ranking_enabled=True)


def _sep(decision):
    return decision.separation_stage, decision.separation_state, decision.best_tier_size


def _leader(decision):
    return decision.ordered[0]


# ---------------------------------------------------------------------------
# Сценарии — функции, чтобы сторож ниже прошёл по каждому
# ---------------------------------------------------------------------------

def _split_at_s2():
    return resolve(make_request(), source=StaticSource([
        make_facts(match_level=MatchLevel.SERVICE_EXACT),
        make_facts(match_level=MatchLevel.SERVICE_PARTIAL),
    ]))


def _split_at_s5():
    return resolve(make_request(), source=StaticSource([
        make_facts(rating=("5.0", 50)),
        make_facts(rating=("4.0", 50)),
    ]), policy=_QUALITY)


def _split_at_s2_then_s5():
    return resolve(make_request(), source=StaticSource([
        make_facts(match_level=MatchLevel.SERVICE_EXACT, rating=("5.0", 50)),
        make_facts(match_level=MatchLevel.SERVICE_EXACT, rating=("4.0", 50)),
        make_facts(match_level=MatchLevel.SERVICE_PARTIAL, rating=("5.0", 50)),
    ]), policy=_QUALITY)


def _best_tier_of_two():
    return resolve(make_request(), source=StaticSource([
        make_facts(match_level=MatchLevel.SERVICE_EXACT),
        make_facts(match_level=MatchLevel.SERVICE_EXACT),
        make_facts(match_level=MatchLevel.SERVICE_PARTIAL),
    ]))


def _all_tied():
    return resolve(make_request(), source=StaticSource([make_facts(), make_facts(), make_facts()]))


def _stages_inactive():
    return resolve(
        make_request(need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT)),
        source=StaticSource([make_facts(), make_facts()]),
    )


def _single():
    return resolve(make_request(), source=StaticSource([make_facts()]))


def _empty():
    return resolve(make_request(), source=StaticSource([]))


def _k5_only_unconfirmed():
    unconfirmed = make_facts(is_bookable=True, schedule_state=ScheduleState.UNCONFIRMED)
    return resolve(make_request(**_K5_REQUEST_KW), source=StaticSource([unconfirmed]), policy=_K5_POLICY)


def _k5_confirmed_and_unconfirmed():
    confirmed = make_facts(is_bookable=True, schedule_state=ScheduleState.CONFIRMED_IN_WINDOW)
    unconfirmed = make_facts(is_bookable=True, schedule_state=ScheduleState.UNCONFIRMED)
    return resolve(
        make_request(**_K5_REQUEST_KW), source=StaticSource([unconfirmed, confirmed]), policy=_K5_POLICY,
    )


_ALL_SCENARIOS = [
    _split_at_s2, _split_at_s5, _split_at_s2_then_s5, _best_tier_of_two, _all_tied,
    _stages_inactive, _single, _empty, _k5_only_unconfirmed, _k5_confirmed_and_unconfirmed,
]


# ---------------------------------------------------------------------------
# SPLIT
# ---------------------------------------------------------------------------

def test_semantic_fit_split_is_named_s2():
    assert _sep(_split_at_s2()) == (StageId.S2, SeparationState.SPLIT, 1)


def test_quality_split_is_named_s5():
    assert _sep(_split_at_s5()) == (StageId.S5, SeparationState.SPLIT, 1)


def test_the_first_splitting_stage_is_named_not_the_last():
    """H1-в дословно: «первой разделила». Лидер различён дважды — на S2 и на S5.

    Положительная стража в том же теле: без неё тест зеленел бы и на данных,
    где S5 лидера вовсе не различала, — и не отличал бы «первую» от «последней».
    """
    decision = _split_at_s2_then_s5()
    assert _leader(decision).stage_verdicts[StageId.S5] is StageVerdict.DISTINGUISHED
    assert _sep(decision) == (StageId.S2, SeparationState.SPLIT, 1)


def test_best_tier_that_is_not_a_single_leader_keeps_its_size():
    """Р2: отделён на S2, но в ярусе двое. Каталог не схлопывает это в «лидера нет» (§13.1 `0.0`)."""
    decision = _best_tier_of_two()
    assert _sep(decision) == (StageId.S2, SeparationState.SPLIT, 2)
    assert decision.best_tier_size == sum(1 for c in decision.ordered if c.tier == 1)


# ---------------------------------------------------------------------------
# Стадии нет — и причина названа
# ---------------------------------------------------------------------------

def test_nobody_distinguished_is_not_split():
    decision = _all_tied()
    assert _sep(decision) == (None, SeparationState.NOT_SPLIT, 3)
    assert decision.best_tier_size == len(decision.ordered)


def test_inactive_stages_do_not_invent_a_stage():
    """R4 и Р3: стадии без данных молчат, и молчание не превращается в «S2»."""
    decision = _stages_inactive()
    activity = {row.stage: row.active for row in decision.stage_activity}
    assert activity[StageId.S2] is False
    assert _sep(decision) == (None, SeparationState.NOT_SPLIT, 2)


def test_single_candidate_has_nothing_to_be_separated_from():
    assert _sep(_single()) == (None, SeparationState.SINGLE_CANDIDATE, 1)


def test_no_candidates_is_named_as_such():
    assert _sep(_empty()) == (None, SeparationState.NO_CANDIDATES, 0)


def test_k5_empty_first_tier_is_not_mistaken_for_a_single_candidate():
    """K5 / §29.5: первого нет. Кандидат один, но состояние — не SINGLE_CANDIDATE."""
    decision = _k5_only_unconfirmed()
    assert decision.ordered[0].tier == 2
    assert _sep(decision) == (None, SeparationState.TIER_ONE_EMPTY, 0)


def test_k5_with_a_confirmed_candidate_is_an_ordinary_split():
    """Положительная стража K5: подтверждённый есть — первый ярус не пуст."""
    decision = _k5_confirmed_and_unconfirmed()
    assert decision.separation_state is SeparationState.SPLIT
    assert decision.separation_stage is StageId.S3
    assert decision.best_tier_size == 1


# ---------------------------------------------------------------------------
# Сторож Р1: числа нет, пока его не решил владелец
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", _ALL_SCENARIOS, ids=lambda f: f.__name__.lstrip("_"))
def test_separation_number_stays_null_until_the_owner_decides(scenario):
    """§13.1 `separation ∈ [0,1]` не выводится из H1-в — ни на решении, ни на проводе.

    Покраснел — значит, кто-то назначил отображение стадии в число. Это
    `CONTROLLED_POLICY` владельца (DRF-1883, черновик поправки канона), а не код каталога.
    """
    decision = scenario()
    payload = decision_to_payload(decision)
    assert decision.separation is None
    assert payload["separation"] is None
    assert decision.separation_stage in (None, StageId.S2, StageId.S3, StageId.S4, StageId.S5)


@pytest.mark.parametrize("scenario", _ALL_SCENARIOS, ids=lambda f: f.__name__.lstrip("_"))
def test_payload_carries_the_stage_as_a_code_and_passes_its_own_schema(scenario):
    decision = scenario()
    payload = decision_to_payload(decision)
    expected_stage = decision.separation_stage.value if decision.separation_stage else None
    assert (payload["separation_stage"], payload["separation_state"], payload["best_tier_size"]) == (
        expected_stage, decision.separation_state.value, decision.best_tier_size,
    )
    serializer = ResolveResponseSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors


def test_spec_version_is_at_least_the_one_that_introduced_the_fields():
    """Поля стадии появились в 1.1.0. Версия, оставшаяся на 1.0, врала бы истории решений.

    `test_version_travels_in_the_body` сравнивает ответ с константой и
    пропустил бы забытый подъём: он проверяет, что версия приехала, а не
    что она верна содержимому. Мажор прежний — потребители разбирают `1.x`.
    """
    major, minor = (int(part) for part in RESOLVER_SPEC_VERSION.split(".")[:2])
    assert major == 1
    assert minor >= 1


def test_the_state_vocabulary_is_closed():
    assert tuple(SeparationState) == (
        "SPLIT", "NOT_SPLIT", "SINGLE_CANDIDATE", "NO_CANDIDATES", "TIER_ONE_EMPTY",
    )
