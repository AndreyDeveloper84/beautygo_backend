"""Словарь «русское название → термин USDA»: данные и два разных промаха (DRF-2381).

## Что чинится

Слой USDA отправлял русское название **как есть**, кириллицей. У источника
такой записи нет ни для одного блюда — слой работал и находил ноль.

## Почему промахов два

`miss` лечится словарём, но только один из двух его видов:

* `reason=unmapped dish=…` — названия нет в словаре; лечится **записью в
  словарь**, и такое название наружу не уходит вовсе (промах известен
  заранее, а запрос стоит квоты владельца);
* `reason=not_in_source query=… source=dict|as_is` — отправили и не нашли;
  лечится другим термином либо признанием, что у источника этого нет.

``source`` в строке обязателен: без него «плохой перевод в словаре» не
отличить от «перевод не отправляли вовсе», а лечатся они по-разному.

## Чего эти узлы НЕ утверждают

Что термины **верны**. Они не сверены с живым ответом USDA: ключа у
исполнителя нет, живой запрос расходует квоту. Узлы держат форму словаря и
поведение слоя, а правильность терминов — отдельный шаг на стенде.
"""

from __future__ import annotations

import logging

import pytest

from nutrition.data.ru_dishes_seed import ALIASES, DISH_MACROS
from nutrition.data.ru_to_usda import RU_TO_USDA, query_for
from nutrition.services.usda_lookup import USDALookup


class TestTheDictionaryHoldsOnlyLiveEntries:
    def test_it_never_overlaps_the_seed(self) -> None:
        """Ноль пересечения — требование, а не наблюдение.

        Сид отвечает раньше слоя, поэтому запись, совпавшая с ним, не
        сработает НИ РАЗУ: она мёртвая и при этом выглядит как покрытие.
        Узел стоит именно затем, чтобы завтра никто не «улучшил покрытие»
        мёртвыми записями.
        """
        seed_names = set(DISH_MACROS) | set(ALIASES)

        # Наличие раньше отсутствия: оба множества непусты, иначе «не
        # пересекаются» — правда о пустоте.
        assert len(seed_names) >= 100, len(seed_names)
        assert len(RU_TO_USDA) >= 30, len(RU_TO_USDA)
        assert not (set(RU_TO_USDA) & seed_names)

    def test_every_entry_carries_a_query_and_an_origin(self) -> None:
        """Запись без происхождения равна выдуманной (урок DRF-2402)."""
        for name, entry in RU_TO_USDA.items():
            assert entry.query.strip(), name
            assert entry.origin == "usda_common_name", name
            # Термин источника — латиницей: кириллица здесь означала бы, что
            # запись ничего не переводит.
            assert all(not ("Ѐ" <= ch <= "ӿ") for ch in entry.query), name

    def test_keys_are_normalised_the_way_the_layer_normalises(self) -> None:
        """Иначе запись есть, а слой её не найдёт — и это будет `unmapped`."""
        for name in RU_TO_USDA:
            assert name == name.strip().casefold(), name

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("курица", "chicken"), ("яйцо", "egg"), ("яблоко", "apples")],
    )
    def test_a_few_entries_say_what_they_should(self, name: str, expected: str) -> None:
        entry = query_for(name)
        assert entry is not None
        assert expected in entry.query

    def test_an_unknown_name_gives_none_not_an_empty_string(self) -> None:
        """`None` — «нет записи»; пустая строка уехала бы в запрос."""
        assert query_for("шаурма по-ассирийски") is None


class TestTheTwoMissesAreTreatedApart:
    def _layer(self, settings) -> USDALookup:
        settings.USDA_API_KEY = "test-key"  # pragma: allowlist secret
        return USDALookup()

    def test_an_unmapped_russian_name_is_not_sent_at_all(
        self, settings, caplog
    ) -> None:
        layer = self._layer(settings)
        sent: list[str] = []
        layer._fetch = lambda key: sent.append(key)  # type: ignore[assignment]

        with caplog.at_level(logging.INFO):
            assert layer.lookup("шаурма по-ассирийски") is None

        # Главное: квота владельца не потрачена на заранее известный промах.
        assert sent == []
        assert "reason=unmapped" in caplog.text
        assert "шаурма по-ассирийски" in caplog.text

    def test_a_mapped_name_goes_out_in_the_source_s_own_words(
        self, settings, caplog, db
    ) -> None:
        layer = self._layer(settings)
        sent: list[str] = []

        def _fetch(key: str) -> None:
            sent.append(key)
            return None

        layer._fetch = _fetch  # type: ignore[assignment]

        with caplog.at_level(logging.INFO):
            assert layer.lookup("курица") is None

        # Наличие раньше отсутствия: запрос ушёл — и ушёл английским.
        assert len(sent) == 1
        assert "chicken" in sent[0]
        assert all(not ("Ѐ" <= ch <= "ӿ") for ch in sent[0])
        # А промах назвался вторым видом — с запросом и его происхождением.
        assert "reason=not_in_source" in caplog.text
        assert "source=dict" in caplog.text
        assert "reason=unmapped" not in caplog.text

    def test_a_latin_name_is_sent_as_is_and_says_so(self, settings, caplog, db) -> None:
        """Человек мог назвать продукт словами источника — словарь не нужен."""
        layer = self._layer(settings)
        sent: list[str] = []

        def _fetch(key: str) -> None:
            sent.append(key)
            return None

        layer._fetch = _fetch  # type: ignore[assignment]

        with caplog.at_level(logging.INFO):
            assert layer.lookup("quinoa uncooked") is None

        assert sent == ["quinoa uncooked"]
        assert "source=as_is" in caplog.text

    def test_the_two_reasons_never_share_one_text(self, settings, caplog, db) -> None:
        """Слить их в общий `miss` значило бы отправить читающего чинить не то."""
        layer = self._layer(settings)
        layer._fetch = lambda key: None  # type: ignore[assignment]

        with caplog.at_level(logging.INFO):
            layer.lookup("шаурма по-ассирийски")
        unmapped = caplog.text
        caplog.clear()

        with caplog.at_level(logging.INFO):
            layer.lookup("курица")
        not_in_source = caplog.text

        assert unmapped and not_in_source
        assert unmapped != not_in_source


class TestTheCacheIsKeyedByWhatWeSend:
    def test_two_russian_names_of_one_product_share_a_cache_row(
        self, settings, db
    ) -> None:
        """«курица» и «куриная грудка» — один термин, значит один запрос.

        Если ключом кэша останется русское название, второй заход уйдёт в
        сеть за тем же самым, и квота потратится дважды на один продукт.
        """
        settings.USDA_API_KEY = "test-key"  # pragma: allowlist secret
        layer = USDALookup()
        calls: list[str] = []
        payload = {
            "fdc_id": 1,
            "description": "Chicken, breast",
            "kcal_per_100g": 120.0,
            "protein_g_per_100g": 22.5,
            "fat_g_per_100g": 2.6,
            "carbs_g_per_100g": 0.0,
        }

        def _fetch(key: str) -> dict:
            calls.append(key)
            return payload

        layer._fetch = _fetch  # type: ignore[assignment]

        first = layer.lookup("курица", portion_g=100)
        second = layer.lookup("куриная грудка", portion_g=100)

        assert first is not None and second is not None
        assert len(calls) == 1, calls
