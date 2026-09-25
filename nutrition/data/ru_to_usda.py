"""Словарь «русское название → поисковый термин USDA» (DRF-2381).

## Зачем он вообще нужен

Слой USDA существует с DRF-2334, но русское название уходило в запрос **как
есть**, кириллицей. У источника такой записи нет ни для одного блюда, поэтому
включение слоя не давало ни одного попадания: слой работал, а находил ноль.

## Что здесь лежит, а что НЕ лежит

**Сид до USDA не доходит.** ``NutritionLookup.lookup()`` сперва спрашивает сид и
при попадании возвращается сразу — слой не вызывается вовсе (узлы
``test_a_seed_hit_never_touches_the_source``, DRF-2334, и
``test_seed_dishes_never_reach_the_source``). Сид — это **54 блюда плюс 75
алиасов, 129 названий**, и вносить их сюда значило бы завести мёртвые записи:
они не сработают ни разу, а выглядеть будут как покрытие.

**Сид состоит из блюд, промахиваются продукты.** Проба на день замера: `рис`,
`творог`, `молоко`, `овсянка`, `сыр`, `хлеб белый`, `гречка` — в сиде; `курица`,
`яйцо`, `яблоко`, `банан`, `лосось` — вне. Поэтому здесь **базовые продукты**, и
пересечение с сидом проверяется узлом: ноль — не наблюдение, а требование.

## Происхождение терминов — и его предел

У каждой записи одно происхождение: ``usda_common_name`` — общепринятое
английское название продукта в номенклатуре USDA (наборы Foundation и SR
Legacy, именно они запрашиваются в ``usda_lookup._fetch``).

Без умолчаний, чем это НЕ является: **термины составлены окном-исполнителем по
соглашениям названий источника и НЕ сверены с живым ответом USDA.** Ключа у
исполнителя нет, а живой запрос расходует квоту владельца. Сверка — отдельный
шаг на стенде, и до неё словарь считается **непроверенным**.

Почему это написано прямо, а не «сверено с источником»: по решению владельца
(§77 п.36) число от USDA — **ответ**, а не контроль, и попадает человеку. Неверный
термин даст не «примерно то же», а **чужое число под видом ответа**. Пометка
происхождения существует ровно для того, чтобы вывод не выглядел фактом.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RU_TO_USDA", "UsdaQuery", "query_for"]


@dataclass(frozen=True)
class UsdaQuery:
    """Поисковый термин для источника и происхождение этого термина.

    ``origin`` — не украшение: запись без происхождения равна выдуманной
    (урок DRF-2286 и типовой порции DRF-2402).
    """

    query: str
    origin: str = "usda_common_name"


def _q(query: str) -> UsdaQuery:
    return UsdaQuery(query=query)


#: Русское название (в нижнем регистре, как его отдаёт ``_normalize``) →
#: термин источника. Ключи НЕ пересекаются с сидом — это проверяется узлом.
RU_TO_USDA: dict[str, UsdaQuery] = {
    # --- Мясо и птица -------------------------------------------------
    "курица": _q("chicken broiler or fryer breast meat only raw"),
    "куриная грудка": _q("chicken broiler or fryer breast meat only raw"),
    "курица отварная": _q("chicken broiler or fryer breast meat only cooked braised"),
    "куриное бедро": _q("chicken broiler or fryer thigh meat only raw"),
    "индейка": _q("turkey breast meat only raw"),
    "говядина": _q("beef loin top loin steak boneless separable lean only raw"),
    "свинина": _q("pork fresh loin center rib chop boneless separable lean only raw"),
    "фарш говяжий": _q("beef ground 85% lean meat 15% fat raw"),
    "печень": _q("beef liver raw"),
    # --- Рыба и морепродукты ------------------------------------------
    "лосось": _q("fish salmon atlantic farmed raw"),
    "форель": _q("fish trout rainbow farmed raw"),
    "сельдь": _q("fish herring atlantic raw"),
    "тунец": _q("fish tuna light canned in water drained solids"),
    "минтай": _q("fish pollock alaska raw"),
    "креветки": _q("crustaceans shrimp raw"),
    # --- Яйца ---------------------------------------------------------
    "яйцо": _q("egg whole raw fresh"),
    "яйца": _q("egg whole raw fresh"),
    "яйцо варёное": _q("egg whole cooked hard-boiled"),
    "яичный белок": _q("egg white raw fresh"),
    # --- Крупы, бобовые, мука -----------------------------------------
    "булгур": _q("bulgur dry"),
    "киноа": _q("quinoa uncooked"),
    "перловка": _q("barley pearled raw"),
    "пшено": _q("millet raw"),
    "фасоль": _q("beans kidney red mature seeds raw"),
    "чечевица": _q("lentils raw"),
    "горох": _q("peas split mature seeds raw"),
    "нут": _q("chickpeas garbanzo beans bengal gram mature seeds raw"),
    # --- Овощи --------------------------------------------------------
    "картофель": _q("potatoes russet flesh and skin raw"),
    "морковь": _q("carrots raw"),
    "капуста": _q("cabbage raw"),
    "брокколи": _q("broccoli raw"),
    "огурец": _q("cucumber with peel raw"),
    "помидор": _q("tomatoes red ripe raw year round average"),
    "лук": _q("onions raw"),
    "перец болгарский": _q("peppers sweet red raw"),
    "свёкла": _q("beets raw"),
    "кабачок": _q("squash summer zucchini includes skin raw"),
    # --- Фрукты и ягоды ------------------------------------------------
    "яблоко": _q("apples raw with skin"),
    "банан": _q("bananas raw"),
    "апельсин": _q("oranges raw all commercial varieties"),
    "мандарин": _q("tangerines mandarin oranges raw"),
    "груша": _q("pears raw"),
    "виноград": _q("grapes red or green european type raw"),
    "клубника": _q("strawberries raw"),
    "малина": _q("raspberries raw"),
    "черника": _q("blueberries raw"),
    "арбуз": _q("watermelon raw"),
    "авокадо": _q("avocados raw all commercial varieties"),
    # --- Орехи и семечки ----------------------------------------------
    "грецкий орех": _q("nuts walnuts english"),
    "миндаль": _q("nuts almonds"),
    "арахис": _q("peanuts all types raw"),
    "семечки": _q("seeds sunflower seed kernels dried"),
    # --- Молочное, чего нет в сиде -------------------------------------
    "масло сливочное": _q("butter without salt"),
    "сливки": _q("cream fluid heavy whipping"),
    "ряженка": _q("milk buttermilk fluid cultured lowfat"),
    # --- Масла и прочее ------------------------------------------------
    "масло растительное": _q("oil sunflower linoleic approx 65%"),
    "оливковое масло": _q("oil olive salad or cooking"),
    "мёд": _q("honey"),
    "сахар": _q("sugars granulated"),
}


def query_for(name: str) -> UsdaQuery | None:
    """Термин источника для русского названия, или ``None``, если его нет.

    ``None`` — не сбой, а честный ответ «этого названия в словаре нет»; звонящий
    обязан сказать это вслух отдельной строкой журнала, иначе непокрытое блюдо
    не отличить от блюда, которого нет у самого источника.
    """
    return RU_TO_USDA.get(name.strip().casefold())
