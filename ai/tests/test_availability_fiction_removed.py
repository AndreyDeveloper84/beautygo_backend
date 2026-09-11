"""Снятие фикции доступности не изменило порядок — доказано прогоном.

Что было снято
--------------

`_score_availability` весил 20% и возвращал `1.0` **всем**: он проверял
`is_booking_enabled`, а кандидаты этим же флагом уже отфильтрованы. Пятая
часть формулы не различала никого, но читалась как работающий механизм.

Почему прогон, а не рассуждение
-------------------------------

Рассуждение звучит убедительно: «константа входит аддитивно с одинаковым
весом, её удаление сдвигает все счета на одну величину, порядок
сохраняется; перенормировка умножает все счета на одно число, порядок
тоже сохраняется».

Оно верно **ровно пока компонент — константа**. Если бы он ею не был
хоть на одном входе, рассуждение рухнуло бы вместе с решением, и заметить
это было бы нечем. Поэтому здесь стоит перебор наборов с разными счетами
и сравнение порядка со **старой формулой, записанной явно**.

Старая формула воспроизведена в `_legacy_composite` — не импортирована,
потому что импортировать больше нечего. Это осознанный дубль: он
существует, чтобы у утверждения «порядок не изменился» был второй
операнд.
"""
from __future__ import annotations

import itertools

import pytest

from ai.application.services.recommendation_engine import (
    WEIGHT_DISTANCE,
    WEIGHT_HISTORY,
    WEIGHT_RATING,
    WEIGHT_SERVICE_MATCH,
    ScoreBreakdown,
)

#: Веса ДО снятия — контракт DRF-105, как он выглядел до §125.
LEGACY = {
    "rating": 0.30,
    "distance": 0.25,
    "availability": 0.20,
    "service_match": 0.15,
    "history": 0.10,
}

#: Значение, которое `_score_availability` возвращал всем без исключения.
LEGACY_AVAILABILITY_CONSTANT = 1.0


def _legacy_composite(rating, distance, service_match, history) -> float:
    """Балл по старой формуле, с константой доступности внутри."""
    return (
        LEGACY["rating"] * rating
        + LEGACY["distance"] * distance
        + LEGACY["availability"] * LEGACY_AVAILABILITY_CONSTANT
        + LEGACY["service_match"] * service_match
        + LEGACY["history"] * history
    )


#: Сетка входов. Крайние значения и середина по каждому измерению —
#: 4^4 = 256 наборов. Не случайные: воспроизводимость важнее объёма,
#: а плавающая выборка дала бы «прошло на этом сиде».
GRID = (0.0, 0.33, 0.67, 1.0)


def _all_candidates():
    for values in itertools.product(GRID, repeat=4):
        yield values


def test_the_weights_still_sum_to_one():
    """Перенормировка не сломала сумму.

    Сумма не косметика: по ней читают, что балл нормирован, и по ней же
    сравнивают вклады в `top_reasons`.
    """
    total = WEIGHT_RATING + WEIGHT_DISTANCE + WEIGHT_SERVICE_MATCH + WEIGHT_HISTORY
    assert abs(total - 1.0) < 1e-9, total


def test_the_ratios_between_remaining_weights_are_unchanged():
    """Отношения оставшихся весов сохранены.

    Перенормировка обязана быть пропорциональной. Сдвинь она хоть одно
    отношение — это была бы уже другая формула, а не та же без
    константы, и порядок мог бы измениться по существу.
    """
    for a, b in itertools.combinations(
        (("rating", WEIGHT_RATING), ("distance", WEIGHT_DISTANCE),
         ("service_match", WEIGHT_SERVICE_MATCH), ("history", WEIGHT_HISTORY)), 2,
    ):
        (name_a, new_a), (name_b, new_b) = a, b
        assert abs(new_a / new_b - LEGACY[name_a] / LEGACY[name_b]) < 1e-9, (
            f"отношение {name_a}/{name_b} изменилось при перенормировке"
        )


def test_order_is_identical_on_every_pair_of_candidates():
    """Порядок ЛЮБОЙ пары кандидатов совпадает со старой формулой.

    Проверяется попарно, а не сортировкой списка: сортировка скрыла бы
    расхождение внутри группы равных, а попарное сравнение показывает
    каждое несовпадение отдельно и называет вход, на котором оно вышло.

    256 наборов × попарно — это и есть «на любом наборе» в той форме, в
    какой её можно предъявить.
    """
    candidates = list(_all_candidates())
    mismatches = []
    for left, right in itertools.combinations(candidates, 2):
        new_left = ScoreBreakdown(*left).composite
        new_right = ScoreBreakdown(*right).composite
        old_left = _legacy_composite(*left)
        old_right = _legacy_composite(*right)

        # Знак разности — это и есть порядок. Сравниваются знаки, а не
        # сами баллы: баллы обязаны отличаться, порядок — нет.
        if _sign(new_left - new_right) != _sign(old_left - old_right):
            mismatches.append((left, right, old_left - old_right, new_left - new_right))

    assert not mismatches, (
        f"порядок изменился на {len(mismatches)} парах, первая: {mismatches[0]}"
    )


def _sign(x: float) -> int:
    if abs(x) < 1e-12:
        return 0
    return 1 if x > 0 else -1


def test_the_scores_themselves_did_change():
    """Положительная стража: баллы всё-таки другие.

    Без неё предыдущий тест зеленел бы и на правке, которая ничего не
    сделала: если бы формула осталась прежней, порядок совпал бы
    тривиально, и «доказательство» доказывало бы отсутствие правки.
    """
    values = (0.9, 0.4, 0.7, 0.2)
    assert ScoreBreakdown(*values).composite != pytest.approx(
        _legacy_composite(*values)
    ), "балл не изменился — правка не применилась"


def test_the_breakdown_no_longer_carries_availability():
    """У разбора балла больше нет поля доступности.

    Структурная проверка рядом с числовыми: поле, оставленное «на
    будущее», через месяц кто-нибудь заполнит константой снова.
    """
    assert not hasattr(ScoreBreakdown(0.5, 0.5, 0.5, 0.5), "availability")


def test_a_weight_named_availability_cannot_come_back_without_an_input():
    """Вес доступности не возвращается, пока нет настоящего входа.

    Сторож на класс дефекта, а не на его экземпляр. Вернуть компонент
    можно только вместе с входом — «есть ли у мастера свободное время»,
    — и это контракт DRF-1637. Пока его нет, любое `WEIGHT_AVAILABILITY`
    в модуле означает, что фикция вернулась под тем же именем.

    Читается ИСХОДНИК, а не атрибуты модуля: константу можно объявить
    так, что `hasattr` её не увидит, а глазами в диффе она мелькнёт
    одной строкой.
    """
    import ast
    import inspect

    from ai.application.services import recommendation_engine

    tree = ast.parse(inspect.getsource(recommendation_engine))
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "WEIGHT_AVAILABILITY" not in assigned, (
        "вес доступности вернулся. Вернуть его можно только вместе с "
        "настоящим входом (DRF-1637): вес без входа — фикция, снятая §125."
    )
