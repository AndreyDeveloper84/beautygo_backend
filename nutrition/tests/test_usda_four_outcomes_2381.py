"""Журнал различает четыре исхода слоя USDA, и «выключено» — среди них (DRF-2381).

## Что чинится

Слой официального справочника молчал, когда выключен. В пустом журнале
«слой выключен» и «слой включён и ничего не нашёл» выглядели одинаково, а
чинятся разными руками: первое — переменной в контуре, второе — словарём
названий. Молчание — не состояние.

Та же беда и та же развязка, что у `booking.auto_complete.pass ran=false`
(DRF-1048): отказ работать говорит вслух и называет переменную, которая
это меняет.

## Почему четыре, а не два

Каждый исход чинится своим действием, поэтому их нельзя сливать:

* `disabled` — решение владельца, чинится переменной;
* `not_configured` — наша недонастройка, чинится ключом;
* `miss` — источник ответил, блюда у него нет (или запрос ушёл кириллицей:
  таблицы «русское название → запись USDA» пока нет вовсе);
* `unavailable` — источник отказал, чинится временем или обращением к ним.

## Чего эти узлы НЕ утверждают

Что включённый слой начнёт находить русские блюда. Не начнёт: словаря
названий нет, запрос уходит как есть. Это отдельная работа, и узлы про неё
живут отдельно — здесь только про то, что состояние **названо**.
"""

from __future__ import annotations

import logging

import pytest

from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup


class TestEveryStateSaysItsName:
    def test_disabled_is_said_out_loud_and_names_its_switch(
        self, settings, caplog
    ) -> None:
        """Раньше здесь было молчание, записанное как замысел."""
        settings.USDA_LOOKUP_ENABLED = False

        with caplog.at_level(logging.INFO):
            lookup = build_nutrition_lookup()

        assert lookup._usda is None
        assert "reason=disabled" in caplog.text
        # Имя переменной в строке — чтобы читающий журнал знал, что крутить,
        # и не искал причину в коде.
        assert "USDA_LOOKUP_ENABLED" in caplog.text

    def test_disabled_is_not_an_alarm(self, settings, caplog) -> None:
        """Сказано — не значит «авария»: уровень INFO, а не WARNING.

        Обратная сторона предыдущего узла. Без неё «состояние названо» можно
        было бы выполнить, разбудив дежурного на штатной выключенности.
        """
        settings.USDA_LOOKUP_ENABLED = False

        with caplog.at_level(logging.WARNING):
            build_nutrition_lookup()

        assert not caplog.records, f"выключённость — не тревога: {caplog.text!r}"

    def test_enabled_without_a_key_is_a_different_line(
        self, settings, caplog
    ) -> None:
        """Недонастройка звучит иначе — её чинит человек, и это WARNING."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = ""

        with caplog.at_level(logging.INFO):
            lookup = build_nutrition_lookup()

        assert lookup._usda is None
        assert "reason=not_configured" in caplog.text
        assert "reason=disabled" not in caplog.text

    def test_the_two_absences_are_never_the_same_line(self, settings, caplog) -> None:
        """Главный узел: два исхода не сливаются в один текст.

        Если завтра кто-то «упростит» причины до общего `layer_absent`,
        читающий журнал снова не отличит решение владельца от нашей
        недонастройки — и пойдёт чинить не то.
        """
        settings.USDA_LOOKUP_ENABLED = False
        with caplog.at_level(logging.INFO):
            build_nutrition_lookup()
        off = caplog.text
        caplog.clear()

        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = ""
        with caplog.at_level(logging.INFO):
            build_nutrition_lookup()
        unkeyed = caplog.text

        assert off and unkeyed
        assert off != unkeyed


class TestTheLayerStillDoesNotSpeakRussian:
    """Включение слоя не равно «начали находить» — и это записано узлом.

    Иначе следующий прочитает зелёный журнал «слой собран» как «USDA
    работает», а русские блюда всё это время будут промахиваться.
    """

    def test_the_query_leaves_in_cyrillic_as_is(self, settings) -> None:
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key"
        sent: list[str] = []

        lookup = build_nutrition_lookup()
        assert lookup._usda is not None

        def _capture(dish_name: str, **kwargs: object) -> None:
            sent.append(dish_name)
            return None

        lookup._usda.lookup = _capture  # type: ignore[assignment]
        lookup.lookup("лаваш с начинкой", portion_g=200)

        # Наличие раньше отсутствия: запрос вообще ушёл — и ушёл кириллицей.
        assert sent == ["лаваш с начинкой"]
        assert any(ord(ch) > 127 for ch in sent[0])

    @pytest.mark.parametrize("dish", ["борщ", "оливье", "гречка"])
    def test_seed_dishes_never_reach_the_source(self, settings, dish: str) -> None:
        """Положительная половина: то, что знает seed, наружу не ходит."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key"
        calls: list[str] = []

        lookup = build_nutrition_lookup()
        assert lookup._usda is not None
        lookup._usda.lookup = lambda name, **kw: calls.append(name)  # type: ignore[assignment]

        facts = lookup.lookup(dish, portion_g=200)

        assert facts is not None
        assert calls == []
