"""Типовая порция блюда — справочные данные, а не догадка модели (DRF-2402).

## Что чинится

Живой проход владельца (DRF-2401): блюдо найдено, калории на 100 г есть, а
порцию провайдер не оценил — и запись отклонялась. Числа считает backend по
справочнику; модель тут не участвует вовсе (решение владельца, ответ 40 и
§77 п.36).

## Границы, которые держат эти узлы

* типовая порция берётся **только** когда провайдер порцию не назвал —
  оценку провайдера она не перебивает **никогда**;
* блюда без общепринятой порции её не получают: пусто — значит спрашиваем,
  как сейчас. Пустое значение честнее выдуманного;
* у каждого числа есть **происхождение** (`typical_portion_source`): число
  без происхождения здесь равно выдуманному;
* вопрос человеку сохраняется — этот лист его не трогает.
"""

from __future__ import annotations

import pytest

from nutrition.data.ru_dishes_seed import DISH_MACROS, DishMacros
from nutrition.services.nutrition_lookup import NutritionLookup

SEED = {
    "борщ": DishMacros(
        49, 1.6, 2.2, 6.7, typical_portion_g=300, typical_portion_source="ru_serving_practice"
    ),
    "безымянное": DishMacros(100, 1.0, 1.0, 1.0),
}


def _lookup() -> NutritionLookup:
    return NutritionLookup(dish_macros=SEED, aliases={})


class TestTheTypicalPortionFillsOnlyTheGap:
    def test_an_unestimated_portion_falls_back_to_the_typical_one(self):
        """Тот самый случай: блюдо есть, порции нет — и запись больше не пустая."""
        facts = _lookup().lookup("борщ", portion_g=None)

        assert facts is not None
        assert facts.portion_g == 300
        assert facts.kcal == pytest.approx(147.0)  # 49 × 3
        assert facts.portion_source == "typical"

    def test_the_providers_estimate_is_never_overridden(self):
        """Порция названа провайдером — типовая молчит, даже если отличается."""
        facts = _lookup().lookup("борщ", portion_g=420)

        assert facts is not None
        assert facts.portion_g == 420
        assert facts.kcal == pytest.approx(205.8)  # 49 × 4.2
        assert facts.portion_source == "given"

    def test_a_dish_without_a_typical_portion_still_asks(self):
        """Пусто — значит спрашиваем, как сейчас: выдумывать нечего."""
        facts = _lookup().lookup("безымянное", portion_g=None)

        assert facts is not None
        assert facts.portion_g is None
        assert facts.kcal is None
        assert facts.portion_source == "unknown"


class TestEveryNumberHasAnOrigin:
    def test_a_filled_portion_carries_its_source(self):
        """Число без происхождения равно выдуманному (урок DRF-2286)."""
        filled = {
            name: m for name, m in DISH_MACROS.items() if m.typical_portion_g is not None
        }

        # Непустой охват: иначе «у всех есть происхождение» — правда о пустом
        # множестве, и ноль расхождений неотличим от нуля проверенного.
        assert len(filled) >= 30, len(filled)
        for name, macros in filled.items():
            assert macros.typical_portion_source != "unknown", name
            assert macros.typical_portion_g > 0, name

    def test_an_empty_portion_claims_no_source(self):
        """Обратная сторона: где порции нет, там и происхождению взяться неоткуда."""
        empty = {name: m for name, m in DISH_MACROS.items() if m.typical_portion_g is None}

        for name, macros in empty.items():
            assert macros.typical_portion_source == "unknown", name


class TestTheCarrierPassesItOn:
    """Признак обязан доезжать до носителя ответа, а не оставаться серверным.

    Сегодня ровно на этом поймали `per_100g` (DRF-2335): поле добавили, а
    носители его не читали. Носителей у этих фактов три — снимок скана
    (мобильный / мини-апп), ручной поиск блюда и клиент бота; здесь правится
    первый, остальные названы в теле PR.
    """

    def test_the_scan_snapshot_carries_the_portion_source(self):
        from types import SimpleNamespace

        from nutrition.serializers import FoodScanResponseSerializer

        scan = SimpleNamespace(
            nutrition={
                "kcal": 147.0,
                "protein_g": 4.8,
                "fat_g": 6.6,
                "carbs_g": 20.1,
                "kcal_per_100g": 49.0,
                "portion_source": "typical",
            }
        )

        block = FoodScanResponseSerializer().get_nutrition(scan)

        assert block is not None
        assert block["portion_source"] == "typical"

    def test_an_older_snapshot_without_the_field_reads_as_unknown(self):
        """Старые сканы поля не несут — и не притворяются измеренными."""
        from types import SimpleNamespace

        from nutrition.serializers import FoodScanResponseSerializer

        scan = SimpleNamespace(nutrition={"kcal": 147.0, "kcal_per_100g": 49.0})

        block = FoodScanResponseSerializer().get_nutrition(scan)

        assert block is not None
        assert block["portion_source"] == "unknown"
