"""Приёмка геокодирования: команда видит то, чего не видит тест.

Тест не может ответить на «есть ли координаты в боевой базе» — он сам
задаёт себе данные. Команда может, потому что читает ту базу, где они
лежат. Здесь проверяется **сама команда**: что она считает то, что
обещает, и что её нули отличимы от «посчитали не то».
"""
from __future__ import annotations

from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from ai.tests.factories import make_specialist

pytestmark = pytest.mark.django_db

CENTER_LAT, CENTER_LON = 53.2007, 45.0046


def _run(**kwargs) -> str:
    out = StringIO()
    call_command("check_distance_readiness", stdout=out, **kwargs)
    return out.getvalue()


def _at(lat_offset: float, name: str):
    s = make_specialist(display_name=name)
    s.location_lat = D(str(CENTER_LAT + lat_offset))
    s.location_lng = D(str(CENTER_LON))
    s.save()
    return s


def test_empty_table_is_not_reported_as_a_result():
    """Пустая таблица даёт те же нули, что отсутствие координат.

    Это разные новости, и команда обязана их различать: иначе первый
    же прогон не на той базе прочитается как «геокодирование не
    сделано».
    """
    report = _run()
    assert "специалистов всего          : 0" in report
    assert "таблица пуста" in report


def test_no_coordinates_is_named_and_counted():
    """Сегодняшнее состояние пилота: мастера есть, координат нет."""
    make_specialist(display_name="Без координат 1")
    make_specialist(display_name="Без координат 2")

    report = _run()
    assert "специалистов всего          : 2" in report
    assert "из них с координатами       : 0" in report
    assert "не различает никого" in report


def test_distinct_count_needs_no_reference_point_when_nobody_has_coordinates():
    """Без координат ответ не зависит от точки отсчёта — и точку не требуют.

    `_distance_to` возвращает `None` при отсутствии ЛЮБОЙ из четырёх
    координат, значит балл одинаков для любого клиента. Требовать
    здесь `--from-lat` значило бы требовать то, что ни на что не
    влияет.
    """
    make_specialist(display_name="Без координат")
    assert "не зависит от точки отсчёта" in _run()


def test_partial_geocoding_is_called_out_loudly():
    """Частичная геокодировка — не прогресс, а направленная ложь.

    Главная находка замера: нейтральное `0.5` есть середина шкалы.
    Команда обязана назвать это явно, а не показать «1 из 2» как шаг
    вперёд.
    """
    _at(0.05, "С координатами")
    make_specialist(display_name="Без координат")

    report = _run(from_lat=CENTER_LAT, from_lon=CENTER_LON)
    assert "ЧАСТИЧНАЯ ГЕОКОДИРОВКА: 1 из 2" in report
    assert "всех или никого" in report


def test_reference_point_is_required_once_coordinates_exist():
    """Точку отсчёта не подставляем молча.

    «Центр города», подставленный по умолчанию, дал бы правдоподобное
    число ни о чём — и его бы процитировали.
    """
    _at(0.05, "С координатами")

    with pytest.raises(CommandError, match="точку отсчёта"):
        _run()


def test_the_count_flips_when_everyone_is_geocoded():
    """Положительная стража: после геокодирования число переворачивается.

    Без неё все проверки выше зеленели бы и на команде, которая всегда
    докладывает «не различает»: доказывалось бы отсутствие
    геокодирования, а не работа счётчика.
    """
    _at(0.01, "Ближний")
    _at(0.10, "Средний")
    _at(0.20, "Дальний")

    report = _run(from_lat=CENTER_LAT, from_lon=CENTER_LON)
    assert "из них с координатами       : 3" in report
    assert "различных баллов расстояния : 3" in report
    assert "координаты есть у всех" in report


def test_subject_is_printed_next_to_the_result():
    """Предмет рядом с числом: ноль без предмета неотличим от промаха."""
    make_specialist(display_name="Хоть кто-то")
    report = _run()
    assert "предмет:" in report
    assert "max_distance_km=" in report
