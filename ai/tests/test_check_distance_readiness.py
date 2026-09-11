"""Приёмка геокодирования: команда видит то, чего не видит тест.

Тест не может ответить на «есть ли координаты в боевой базе» — он сам
задаёт себе данные. Команда может, потому что читает ту базу, где они
лежат. Здесь проверяется **сама команда**: что она считает то, что
обещает, и что её нули отличимы от «посчитали не то».
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from ai.tests.factories import make_specialist, make_user
from core.measurement_subject import PULSE_ANCHORS, gather_pulse
from users.models import User

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


# ---------------------------------------------------------------------------
# Кто ответил на замер
# ---------------------------------------------------------------------------
#
# 11.09.2026: машин две, совпадает всё кроме адреса, у брошенной копии БД
# осталась жива. Запрос туда не падает — он ОТВЕЧАЕТ замороженным числом.
# Значит команда обязана печатать не только число, но и того, кто его дал.


def _run_full(**kwargs) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("check_distance_readiness", stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def test_subject_is_printed_before_any_number():
    """Предмет стоит ПЕРЕД счётом, а не в подписи под ним.

    Порядок — часть смысла: число, прочитанное раньше, чем «кто его
    выдал», уже процитировано.
    """
    make_specialist(display_name="Хоть кто-то")
    report, _ = _run_full()

    assert report.index("== ПРЕДМЕТ") < report.index("специалистов всего")
    assert "СТАРТ ПРОЦЕССА БД" in report
    assert "ПУЛЬС" in report
    # Чего изнутри не видно — названо, а не пропущено молча.
    assert "изнутри НЕ видно" in report


def test_pulse_anchor_set_resolves_to_real_models():
    """Положительная стража на сам набор опор.

    Опечатка в `"ai.Mesage"` не сделала бы пульс пустым — она сделала бы
    его ТИХИМ, и «ни одна опора не ответила» читалось бы как приговор
    машине вместо приговора набору. Здесь набор проверяется целиком.
    """
    pulses = gather_pulse()
    assert [p.label for p in pulses] == [a.label for a in PULSE_ANCHORS]
    assert all(p.error is None for p in pulses), [p.error for p in pulses]


def test_empty_pulse_is_not_reported_as_fresh():
    """Молчание всех опор — не «свежо», а «не подтверждено».

    Это тот же класс, что пустой прогон тестов: ноль отказов при нуле
    проверок читается как зелень.
    """
    report, err = _run_full()
    assert "НИ ОДНА опора не ответила" in report

    with pytest.raises(SystemExit) as exc:
        _run_full(max_age_hours=1)
    assert exc.value.code == 2


def test_stale_pulse_stops_the_measurement_with_its_own_exit_code():
    """Замороженная копия отвечает охотно — останавливает возраст записи.

    Код 2, а не 1: «геокодирование не готово» — новость про данные,
    «мерю не ту машину» — новость про то, что данных вообще не было.
    """
    user = make_user()
    User.objects.filter(pk=user.pk).update(
        last_login=timezone.now() - timedelta(days=8)
    )

    with pytest.raises(SystemExit) as exc:
        _run_full(max_age_hours=24)
    assert exc.value.code == 2


def test_fresh_pulse_passes_the_same_threshold():
    """Положительная стража: порог пропускает живой контур.

    Без неё предыдущий тест зеленел бы и на команде, которая падает
    всегда, — доказывалась бы не проверка, а её постоянный отказ.
    """
    user = make_user()
    User.objects.filter(pk=user.pk).update(last_login=timezone.now())

    report, _ = _run_full(max_age_hours=24)
    assert "специалистов всего" in report


def test_printed_pulse_and_checked_pulse_are_one_snapshot():
    """Напечатанный возраст и проверенный возраст — из одного мгновения.

    Два отдельных сбора дали бы отчёт про одну отметку времени и отказ
    про другую, и разойтись они могли бы незаметно. Здесь сверяется, что
    в обеих строках стоит одна и та же отметка.
    """
    user = make_user()
    stale = timezone.now() - timedelta(days=8)
    User.objects.filter(pk=user.pk).update(last_login=stale)

    out, err = StringIO(), StringIO()
    with pytest.raises(SystemExit):
        call_command(
            "check_distance_readiness", stdout=out, stderr=err, max_age_hours=24,
        )

    printed = out.getvalue()
    refused = err.getvalue()
    assert "ПУЛЬС (новейшая запись)" in printed
    # Одна и та же отметка в отчёте и в отказе.
    stamp = stale.isoformat()
    assert stamp in printed or stale.strftime("%Y-%m-%d %H:%M") in printed
    assert stamp in refused
