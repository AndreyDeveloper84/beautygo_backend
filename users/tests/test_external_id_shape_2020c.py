"""Первый слой: в текст исключения попадает ФОРМА, а не личность (DRF-2020 C).

Почему файл появился отдельно и после ревью: у первого слоя не было **ни
одного** узла. Проверка подменой это показала беспощадно — я вернул все три
файла слоя к состоянию до листа (0 вызовов `external_id_shape`), и все узлы
листа остались зелёными. То есть слой, чья единственная работа — не печатать
человека, держался обещанием.

Второй слой (редактура события) его прикрывает, и именно поэтому он и написан.
Но прикрытие — не причина не проверять: редактура стоит на пути в Sentry и в
журнал, а текст исключения ходит и в тело ответа 400 (`nutrition/views.py`, 20
мест: `f"X-External-User-ID невалиден: {exc}"`), где никакой редактуры нет. Там
работает только первый слой.

Доктесты в докстринге `external_id_shape` не считаются: `--doctest-modules` нет
ни в `pytest.ini`, ни в задании CI — их никто не исполняет.
"""
from __future__ import annotations

import pytest

from users.services import (
    InvalidExternalUserIDError,
    external_id_shape,
    resolve_external_user,
)

#: Форма настоящая, значение синтетическое.
IDENTITY = "max:729481"


class TestTheShapeOfEveryBranch:
    """Все четыре ветви — таблицей. Ветвь без узла — ветвь, которую перепишут."""

    @pytest.mark.parametrize(
        "value,expected,why",
        [
            (None, "<пусто>", "ничего не пришло"),
            ("", "<пусто>", "пришла пустая строка"),
            ("729481", "<без источника, 6 симв.>", "двоеточия нет вовсе"),
            ("max:729481", "max:<6 симв.>", "канал назван, человек — нет"),
            ("bot:max:729481", "bot:<3 симв.>:<6 симв.>", "трёхчастная, как в продукте"),
            ("MAX:729481", "<источник не распознан, 3:6 симв.>", "источник не как канал"),
            ("729481:max", "<источник не распознан, 6:3 симв.>", "на месте канала цифры"),
        ],
    )
    def test_the_shape_names_the_channel_and_hides_the_person(self, value, expected, why):
        assert external_id_shape(value) == expected, why

    @pytest.mark.parametrize(
        "value",
        ["max:729481", "bot:max:729481", "729481", "MAX:729481", "729481:max"],
    )
    def test_no_branch_ever_prints_the_person(self, value):
        """Отрицательное утверждение отдельно от положительного.

        Таблица выше пришпилена к точным строкам и потому ломается от любой
        правки формулировки. Это — про свойство: цифры человека не появляются
        ни в одной ветви, как бы ни менялась проза.
        """
        shape = external_id_shape(value)

        person = value.split(":")[-1]
        assert person not in shape, f"{value!r} → {shape!r} печатает человека"
        assert str(len(person)) in shape, f"{value!r} → {shape!r} потеряло длину"


class TestTheLivePathThroughTheResolver:
    """Не функция, а путь: текст, который РОЖДАЕТ продукт.

    Узел на `external_id_shape` сам по себе зелен и при нулевом числе вызовов —
    ровно так слой и оказался непроверенным. Здесь падает настоящий резолвер, и
    проверяется текст его исключения: то самое, что уезжает в Sentry и в тело
    400.
    """

    @pytest.mark.django_db
    def test_the_resolver_refuses_without_naming_the_person(self):
        bad = f"{IDENTITY}:слишком:много:частей:!"

        with pytest.raises(InvalidExternalUserIDError) as caught:
            resolve_external_user(bad)

        text = str(caught.value)
        assert "729481" not in text, f"личность в тексте исключения: {text!r}"
        # Положительная сторона: отказ назвал СЕБЯ, иначе «личности нет» было бы
        # правдой и о пустом сообщении.
        assert "симв." in text, f"в тексте нет формы — диагностика потеряна: {text!r}"
