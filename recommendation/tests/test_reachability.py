"""Аналитическая достижимость — T5 (DRF-1566).

Тесты проверяют арифметику, а не выборку: вся ценность приёма в том, что
недостижимость **доказывается**, а не оценивается прогонами. Логов нет,
трафика нет, объекта `Recommendation` нет — а число есть.
"""
from __future__ import annotations

import pytest

from recommendation._reachability import (
    exposure_gap,
    unreachable_with_fixed_order,
    unreachable_with_rotation,
)


def test_fixed_tie_break_hides_everyone_past_the_cut():
    """Сегодняшний домашний экран: один порядок на всех людей.

    Ничья разводится `str(id)`, срез стоит после неё, значит мастер за
    позицией три не показывается **никому и никогда** — не «реже», а
    ни одному человеку ни разу. Ровно это канон §9.1 запрещает словами.
    """
    report = unreachable_with_fixed_order(["a", "b", "c", "d", "e"], k=3)
    assert report.unreachable == ("d", "e")
    assert report.unreachable_count == 2


def test_rotation_hides_only_those_a_stage_actually_outranked():
    """Резолвер: недостижим тот, над кем уже k кандидатов старших ярусов.

    Внутри яруса ротация переставляет, поэтому один ярус из пяти при k=3
    не делает недостижимым никого: место в тройке достаётся разным людям
    при разных seed.
    """
    single_tier = {name: 1 for name in ("a", "b", "c", "d", "e")}
    assert unreachable_with_rotation(single_tier, k=3).unreachable == ()


def test_rotation_cannot_lift_a_candidate_over_a_stage_verdict():
    """Ротация не переранжирует: три кандидата первого яруса закрывают тройку."""
    tiers = {"a": 1, "b": 1, "c": 1, "d": 2, "e": 3}
    report = unreachable_with_rotation(tiers, k=3)
    assert report.unreachable == ("d", "e")


def test_tier_boundary_is_counted_by_strictly_higher_tiers():
    """Кандидат второго яруса достижим, пока над ним меньше k."""
    tiers = {"a": 1, "b": 1, "c": 2, "d": 2}
    assert unreachable_with_rotation(tiers, k=3).unreachable == ()
    assert set(unreachable_with_rotation(tiers, k=2).unreachable) == {"c", "d"}


def test_exposure_gap_names_the_people_nobody_sees_today():
    """Цена лексикографической ничьи — поимённо, а не «примерно».

    Это и есть before/after для C-01: люди, которых сегодня не видит
    никто, а после границы увидит кто-то.
    """
    ids = ["a", "b", "c", "d", "e"]
    fixed = unreachable_with_fixed_order(ids, k=3)
    rotation = unreachable_with_rotation({name: 1 for name in ids}, k=3)
    assert exposure_gap(rotation=rotation, fixed=fixed) == ("d", "e")


def test_no_gap_when_stages_genuinely_distinguished():
    """Если стадии различили — обе модели прячут одних и тех же.

    Важное отрицательное свойство: приём не объявляет ротацию лучше
    вообще. Там, где порядок заслужен, разницы нет, и число это покажет.
    """
    ids = ["a", "b", "c", "d"]
    fixed = unreachable_with_fixed_order(ids, k=2)
    rotation = unreachable_with_rotation({"a": 1, "b": 2, "c": 3, "d": 4}, k=2)
    assert exposure_gap(rotation=rotation, fixed=fixed) == ()


def test_share_is_reported_for_scale():
    report = unreachable_with_fixed_order(list("abcd"), k=1)
    assert report.unreachable_count == 3
    assert report.unreachable_share == 0.75
    assert "3 из 4" in report.summary()


@pytest.mark.parametrize("k", [0, -1])
def test_k_below_one_is_not_a_request(k):
    with pytest.raises(ValueError):
        unreachable_with_fixed_order(["a"], k=k)
    with pytest.raises(ValueError):
        unreachable_with_rotation({"a": 1}, k=k)


def test_empty_pool_reports_nothing_rather_than_claiming_everyone_reachable():
    """Пустой пул — отсутствие замера, а не «все достижимы».

    Команда на этом месте отказывается печатать ноль: разница та же, что
    между «никого нет» и «мы не искали».
    """
    report = unreachable_with_rotation({}, k=3)
    assert report.total == 0
    assert report.unreachable == ()
    assert report.unreachable_share == 0.0
