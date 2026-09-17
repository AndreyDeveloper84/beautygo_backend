"""H1-в: стадия, которая первой разделила лучший ярус (DRF-1934).

Решение владельца 15.09 (`docs/OWNER_QUESTIONS_2026-09-12.md` §H1): «в, считает
каталог». Форма — утверждённая поправка O1
(`docs/PROMPT_ORCHESTRATOR_AYLA_CONTROLLED_PILOT_NEXT_WAVE.md` §8):

* ровно `separation_stage`, `best_group_size`, `candidate_count`,
  `separation_state`, `separation_score`;
* `separation_stage` — категориальная стадия; отсутствие называется явным
  состоянием, а не угадывается;
* O2 (§9): `separation_score` = NULL до калибровки по тени; номер стадии
  в «вероятность» не превращается (§14).

Кандидаты собраны теми же фабриками, что в `test_pipeline`: стадия, названная
здесь, должна быть той же, что там ставит вердикты.
"""
from __future__ import annotations

import pytest

from recommendation._serializers import ResolveResponseSerializer, decision_to_payload
from recommendation.api import (
    RESOLVER_SPEC_VERSION,
    Constraint,
    MappingStatus,
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

#: O1 §8 — имена полей дословно. Пара с поправкой владельца: менять только вместе с ней.
_O1_FIELDS = ("separation_stage", "best_group_size", "candidate_count", "separation_state", "separation_score")
#: Имена из черновика до утверждения O1. На проводе их быть не должно: два имени
#: одной величины — это два контракта.
_SUPERSEDED_FIELDS = ("best_tier_size", "separation")


def _sep(decision):
    return (
        decision.separation_stage, decision.separation_state,
        decision.best_group_size, decision.candidate_count,
    )


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


def _best_group_of_two():
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


def _excluded_candidate_is_not_counted():
    return resolve(make_request(), source=StaticSource([
        make_facts(match_level=MatchLevel.SERVICE_EXACT),
        make_facts(match_level=MatchLevel.SERVICE_PARTIAL),
        make_facts(mapping_status=MappingStatus.UNMAPPED),
    ]))


_ALL_SCENARIOS = [
    _split_at_s2, _split_at_s5, _split_at_s2_then_s5, _best_group_of_two, _all_tied,
    _stages_inactive, _single, _empty, _k5_only_unconfirmed, _k5_confirmed_and_unconfirmed,
    _excluded_candidate_is_not_counted,
]


# ---------------------------------------------------------------------------
# SPLIT
# ---------------------------------------------------------------------------

def test_semantic_fit_split_is_named_s2():
    assert _sep(_split_at_s2()) == (StageId.S2, SeparationState.SPLIT, 1, 2)


def test_quality_split_is_named_s5():
    assert _sep(_split_at_s5()) == (StageId.S5, SeparationState.SPLIT, 1, 2)


def test_the_first_splitting_stage_is_named_not_the_last():
    """H1-в дословно: «первой разделила». Лидер различён дважды — на S2 и на S5.

    Положительная стража в том же теле: без неё тест зеленел бы и на данных,
    где S5 лидера вовсе не различала, — и не отличал бы «первую» от «последней».
    """
    decision = _split_at_s2_then_s5()
    assert _leader(decision).stage_verdicts[StageId.S5] is StageVerdict.DISTINGUISHED
    assert _sep(decision) == (StageId.S2, SeparationState.SPLIT, 1, 3)


def test_best_group_that_is_not_a_single_leader_keeps_its_size():
    """Отделён на S2, но в ярусе двое. Каталог не схлопывает это в «лидера нет»."""
    decision = _best_group_of_two()
    assert _sep(decision) == (StageId.S2, SeparationState.SPLIT, 2, 3)
    assert decision.best_group_size == sum(1 for c in decision.ordered if c.tier == 1)


# ---------------------------------------------------------------------------
# Стадии нет — и причина названа
# ---------------------------------------------------------------------------

def test_nobody_distinguished_is_not_split():
    decision = _all_tied()
    assert _sep(decision) == (None, SeparationState.NOT_SPLIT, 3, 3)


def test_inactive_stages_do_not_invent_a_stage():
    """R4: стадии без данных молчат, и молчание не превращается в «S2»."""
    decision = _stages_inactive()
    activity = {row.stage: row.active for row in decision.stage_activity}
    assert activity[StageId.S2] is False
    assert _sep(decision) == (None, SeparationState.NOT_SPLIT, 2, 2)


def test_single_candidate_has_nothing_to_be_separated_from():
    assert _sep(_single()) == (None, SeparationState.SINGLE_CANDIDATE, 1, 1)


def test_no_candidates_is_named_as_such():
    assert _sep(_empty()) == (None, SeparationState.NO_CANDIDATES, 0, 0)


def test_k5_empty_first_tier_is_not_mistaken_for_a_single_candidate():
    """K5 / §29.5: первого нет. Кандидат один, но состояние — не SINGLE_CANDIDATE."""
    decision = _k5_only_unconfirmed()
    assert decision.ordered[0].tier == 2
    assert _sep(decision) == (None, SeparationState.TIER_ONE_EMPTY, 0, 1)


def test_k5_with_a_confirmed_candidate_is_an_ordinary_split():
    """Положительная стража K5: подтверждённый есть — первый ярус не пуст."""
    decision = _k5_confirmed_and_unconfirmed()
    assert _sep(decision) == (StageId.S3, SeparationState.SPLIT, 1, 2)


def test_candidate_count_is_the_admitted_set_not_everything_seen():
    """Знаменатель к `best_group_size` — допущенные к ранжированию, а не увиденные.

    Исключённый на S0/S1 в ярусы не входит; посчитай его — доля лучшего яруса
    занижалась бы ровно на размер каталога, который рекомендовать нельзя.
    """
    decision = _excluded_candidate_is_not_counted()
    assert decision.census.visible == 3
    assert len(decision.excluded) == 1
    assert _sep(decision) == (StageId.S2, SeparationState.SPLIT, 1, 2)
    assert decision.candidate_count == len(decision.ordered)


# ---------------------------------------------------------------------------
# O2: числа нет, пока его не откалибровали
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", _ALL_SCENARIOS, ids=lambda f: f.__name__.lstrip("_"))
def test_separation_score_stays_null_until_calibrated(scenario):
    """O2 (§9): `separation_score = NULL` до калибровки — ни на решении, ни на проводе.

    Покраснел — значит, кто-то назначил отображение стадии в число, то есть
    превратил порядковый номер в «вероятность» (§14). Это решает владелец по
    данным тени (DRF-1883), а не код каталога.
    """
    decision = scenario()
    payload = decision_to_payload(decision)
    assert decision.separation_score is None
    assert payload["separation_score"] is None
    assert decision.separation_stage in (None, StageId.S2, StageId.S3, StageId.S4, StageId.S5)


@pytest.mark.parametrize("scenario", _ALL_SCENARIOS, ids=lambda f: f.__name__.lstrip("_"))
def test_payload_carries_the_o1_fields_and_passes_its_own_schema(scenario):
    decision = scenario()
    payload = decision_to_payload(decision)
    expected_stage = decision.separation_stage.value if decision.separation_stage else None
    assert {name: payload[name] for name in _O1_FIELDS} == {
        "separation_stage": expected_stage,
        "best_group_size": decision.best_group_size,
        "candidate_count": decision.candidate_count,
        "separation_state": decision.separation_state.value,
        "separation_score": None,
    }
    serializer = ResolveResponseSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors


def test_o1_field_names_are_pinned_on_the_decision_the_wire_and_the_schema():
    """Пин имён O1 §8 и отсутствие имён черновика — на трёх носителях сразу."""
    payload = decision_to_payload(_split_at_s2())
    schema_fields = set(ResolveResponseSerializer().fields)
    decision_fields = set(type(_split_at_s2()).__dataclass_fields__)
    for carrier in (set(payload), schema_fields, decision_fields):
        assert set(_O1_FIELDS) <= carrier
        assert not set(_SUPERSEDED_FIELDS) & carrier


def test_spec_version_is_at_least_the_one_that_introduced_the_fields():
    """Поля O1 появились в 1.1.0. Версия, оставшаяся на 1.0, врала бы истории решений.

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
