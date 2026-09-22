"""Официальный источник подключён в бою, и его нельзя снова отключить молча (DRF-2334).

Замер DRF-2329: `NutritionLookup` по докстроке трёхслойная, а все четыре боевых
места собирали её пустым конструктором — в пилоте работал только seed из
54 блюд. Модули `usda_lookup.py` и `micronutrient_estimator.py` не вызывались
никогда. Ответ владельца на вопрос 40 («справочник → официальный источник →
расчёт по ингредиентам») описывал цепочку, которая обрывалась на первом звене.

Что держат узлы ниже:

* боевая сборка идёт через одну дверь и несёт слой источника;
* **никто не собирает справочник мимо этой двери** — иначе правка развалится
  так же молча, как развалилась прежняя;
* источник выключен по умолчанию; выключенный ведёт себя как «слоя нет» и
  **аварией не считается**;
* ненастроенность и настоящий отказ — разные вещи: первое не будит тревогу,
  второе говорит вслух, и ни то ни другое не превращает скан в 5xx. Общий
  предохранитель бота считает стойкий 5xx аварией — накормить его штатным
  отказом внешнего справочника нельзя.

Слой ИИ-оценки здесь НЕ подключается: по слову владельца ИИ распознаёт
входные данные, а считает backend. Роль `micronutrient_estimator` — отдельный
вопрос, узел ниже держит, что в бою его сегодня нет.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nutrition.services.nutrition_lookup import NutritionFacts
from nutrition.services.usda_lookup import USDAUnavailableError

pytestmark = pytest.mark.django_db

#: Корни боевого кода питания. Тесты сюда не входят — им сборка руками нужна.
_PRODUCTION_ROOTS = (
    Path(__file__).resolve().parent.parent,  # nutrition/
)

#: Единственное место, которому позволено звать конструктор напрямую.
_FACTORY = "nutrition/services/nutrition_lookup_factory.py"


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in _PRODUCTION_ROOTS:
        for path in root.rglob("*.py"):
            parts = path.parts
            if "tests" in parts or "migrations" in parts:
                continue
            files.append(path)
    return files


def _direct_constructions(path: Path) -> list[int]:
    """Строки, где зовут ``NutritionLookup(...)`` напрямую."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name == "NutritionLookup":
            hits.append(node.lineno)
    return hits


class TestNobodyBuildsTheCatalogueBehindTheFactory:
    """Сторож переписи: боевое место собирает справочник только через дверь."""

    def test_no_production_file_calls_the_constructor_directly(self) -> None:
        offenders = {
            str(path): lines
            for path in _production_files()
            if (lines := _direct_constructions(path))
            and not str(path).replace("\\", "/").endswith(_FACTORY)
        }

        assert not offenders, (
            "боевая сборка справочника в обход фабрики — слой источника "
            f"снова потеряется молча: {offenders}"
        )

    def test_the_factory_itself_is_where_the_source_is_attached(self) -> None:
        from nutrition.services import nutrition_lookup_factory as factory

        assert hasattr(factory, "build_nutrition_lookup")


class TestTheSourceIsAttachedWhenTurnedOn:
    def test_the_layer_is_present_when_enabled_and_keyed(self, settings) -> None:
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        assert build_nutrition_lookup()._usda is not None

    def test_a_dish_outside_the_seed_reaches_the_source(self, settings) -> None:
        """Ради чего лист: лаваша нет в seed — за числами идём к источнику."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        usda.lookup.return_value = _facts()
        with patch(
            "nutrition.services.nutrition_lookup_factory.USDALookup",
            return_value=usda,
        ):
            facts = build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200)

        assert facts is not None
        assert facts.source == "usda"
        assert usda.lookup.called

    def test_a_seed_hit_never_touches_the_source(self, settings) -> None:
        """Контроль: порядок слоёв — дешёвое первым, деньги последними."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        with patch(
            "nutrition.services.nutrition_lookup_factory.USDALookup",
            return_value=usda,
        ):
            facts = build_nutrition_lookup().lookup("борщ", portion_g=200)

        assert facts.source == "seed_ru"
        assert not usda.lookup.called


class TestOffByDefaultAndNotAnEmergency:
    def test_the_layer_is_absent_by_default(self, settings) -> None:
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        assert getattr(settings, "USDA_LOOKUP_ENABLED", False) is False
        assert build_nutrition_lookup()._usda is None

    def test_turned_off_behaves_exactly_like_no_layer(self, settings) -> None:
        settings.USDA_LOOKUP_ENABLED = False
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        assert build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200) is None

    def test_turned_off_is_not_an_emergency(self, settings, caplog) -> None:
        settings.USDA_LOOKUP_ENABLED = False
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        with caplog.at_level(logging.WARNING):
            build_nutrition_lookup()

        assert not caplog.records, f"выключенный слой — не авария: {caplog.text!r}"

    def test_enabled_without_a_key_says_so_and_still_does_not_alarm(
        self, settings, caplog,
    ) -> None:
        """Ненастроенность слышно, но она не тревога и не 5xx."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = ""
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        with caplog.at_level(logging.WARNING):
            lookup = build_nutrition_lookup()

        assert lookup._usda is None
        assert "reason=not_configured" in caplog.text


class TestTheSourceNeverFeedsTheCommonBreaker:
    """Штатный отказ внешнего справочника не превращается в нашу аварию."""

    def test_an_outage_of_the_source_is_not_an_outage_of_the_scan(
        self, settings,
    ) -> None:
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        usda.lookup.side_effect = USDAUnavailableError("5xx: 503")
        with patch(
            "nutrition.services.nutrition_lookup_factory.USDALookup",
            return_value=usda,
        ):
            facts = build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200)

        assert facts is None, "отказ источника — пустое питание, а не исключение наверх"

    def test_an_outage_is_said_out_loud(self, settings, caplog) -> None:
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        usda.lookup.side_effect = USDAUnavailableError("5xx: 503")
        with (
            patch(
                "nutrition.services.nutrition_lookup_factory.USDALookup",
                return_value=usda,
            ),
            caplog.at_level(logging.WARNING, logger="nutrition.services.nutrition_lookup"),
        ):
            build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200)

        assert "nutrition.usda.unavailable" in caplog.text

    def test_a_clean_miss_of_the_source_is_not_an_outage(
        self, settings, caplog,
    ) -> None:
        """«Источник не знает такого блюда» — не поломка источника."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        usda.lookup.return_value = None
        with (
            patch(
                "nutrition.services.nutrition_lookup_factory.USDALookup",
                return_value=usda,
            ),
            caplog.at_level(logging.WARNING, logger="nutrition.services.nutrition_lookup"),
        ):
            facts = build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200)

        assert facts is None
        assert "unavailable" not in caplog.text

    def test_the_dish_text_never_reaches_the_log(self, settings, caplog) -> None:
        """Правило DRF-2335: журнал общий, названия блюда в нём нет."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        usda = MagicMock()
        usda.lookup.side_effect = USDAUnavailableError("5xx: 503")
        with (
            patch(
                "nutrition.services.nutrition_lookup_factory.USDALookup",
                return_value=usda,
            ),
            caplog.at_level(logging.DEBUG),
        ):
            build_nutrition_lookup().lookup("лаваш с начинкой", portion_g=200)

        assert "лаваш" not in caplog.text.lower()


class TestTheAILayerStaysOut:
    def test_the_estimator_is_not_wired_in_production(self, settings) -> None:
        """ИИ распознаёт входные данные, считает backend — п. 2 листа за владельцем."""
        settings.USDA_LOOKUP_ENABLED = True
        settings.USDA_API_KEY = "test-key-2334"  # pragma: allowlist secret
        from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup

        assert build_nutrition_lookup()._ai is None


def _facts() -> NutritionFacts:
    return NutritionFacts(
        matched_dish="flatbread, filled",
        source="usda",
        portion_g=200.0,
        kcal_per_100g=250.0,
        protein_g_per_100g=9.0,
        fat_g_per_100g=8.0,
        carbs_g_per_100g=35.0,
        kcal=500.0,
        protein_g=18.0,
        fat_g=16.0,
        carbs_g=70.0,
    )
