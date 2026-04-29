"""Tests for nutrition.services.nutrition_lookup.

Covers:
- Direct canonical hits
- Alias resolution
- ё→е normalization, casing, punctuation, whitespace
- Token-prefix fallback (e.g. "борщ московский" → "борщ")
- Ingredients fallback (only when dish name didn't resolve)
- Portion scaling math + rounding
- Unknown dish + empty input → None
- to_dict shape (consumed by FoodScan.nutrition JSON)
"""
from __future__ import annotations

import pytest

from nutrition.services.nutrition_lookup import (
    NutritionLookup,
    _normalize,
)


# ---------------------------------------------------------------------------
# Normalization helper
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercases(self):
        assert _normalize("БОРЩ") == "борщ"

    def test_yo_becomes_e(self):
        assert _normalize("Сёмга") == "семга"

    def test_strips_punctuation(self):
        assert _normalize("борщ, красный!") == "борщ красный"

    def test_preserves_hyphen(self):
        # котлета по-киевски has a hyphen — keep it for canonical match
        assert _normalize("Котлета по-киевски") == "котлета по-киевски"

    def test_collapses_whitespace(self):
        assert _normalize("  борщ   красный  ") == "борщ красный"

    def test_empty_input(self):
        assert _normalize("") == ""
        assert _normalize("   ") == ""


# ---------------------------------------------------------------------------
# Direct canonical hits
# ---------------------------------------------------------------------------


class TestCanonicalHits:
    def setup_method(self):
        self.svc = NutritionLookup()

    def test_borscht_direct(self):
        f = self.svc.lookup("борщ", portion_g=300)
        assert f is not None
        assert f.matched_dish == "борщ"
        assert f.source == "seed_ru"

    def test_uppercase(self):
        f = self.svc.lookup("БОРЩ", portion_g=300)
        assert f is not None
        assert f.matched_dish == "борщ"

    def test_yo_letter(self):
        # No direct seed entry; ё→е shouldn't change anything for борщ
        # but verify the path works on a known canonical with ё-able
        # rendering.
        f = self.svc.lookup("Гречка", portion_g=200)
        assert f is not None
        assert f.matched_dish == "гречка"

    def test_unknown_returns_none(self):
        assert self.svc.lookup("суши с лососем") is None

    def test_empty_dish_name_no_ingredients_returns_none(self):
        assert self.svc.lookup("") is None

    def test_kotleta_po_kievski_with_punctuation(self):
        f = self.svc.lookup("Котлета по-киевски,", portion_g=180)
        assert f is not None
        assert f.matched_dish == "котлета по-киевски"


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


class TestAliases:
    def setup_method(self):
        self.svc = NutritionLookup()

    @pytest.mark.parametrize("alias,canonical", [
        ("салат оливье", "оливье"),
        ("украинский борщ", "борщ"),
        ("сборная солянка", "солянка"),
        ("шуба", "сельдь под шубой"),
        ("греческий", "греческий салат"),
        ("плов с курицей", "плов"),
        ("спагетти", "макароны"),
        ("капучино", "кофе"),
        ("молочный шоколад", "шоколад"),
        ("ленивые голубцы", "голубцы"),
    ])
    def test_alias_resolves(self, alias, canonical):
        f = self.svc.lookup(alias, portion_g=100)
        assert f is not None, f"alias {alias!r} did not resolve"
        assert f.matched_dish == canonical


# ---------------------------------------------------------------------------
# Token-prefix fallback
# ---------------------------------------------------------------------------


class TestTokenPrefix:
    def setup_method(self):
        self.svc = NutritionLookup()

    def test_first_token_canonical(self):
        # "борщ московский" has no exact alias — first token "борщ" hits.
        f = self.svc.lookup("борщ московский", portion_g=300)
        assert f is not None
        assert f.matched_dish == "борщ"

    def test_first_token_alias(self):
        # "капучино с корицей" → "капучино" alias → "кофе"
        f = self.svc.lookup("капучино с корицей", portion_g=200)
        assert f is not None
        assert f.matched_dish == "кофе"

    def test_unknown_first_token_falls_through(self):
        # "пицца маргарита" — no token resolves → None
        assert self.svc.lookup("пицца маргарита") is None


# ---------------------------------------------------------------------------
# Ingredients fallback (only when dish name didn't resolve)
# ---------------------------------------------------------------------------


class TestIngredientsFallback:
    def setup_method(self):
        self.svc = NutritionLookup()

    def test_unknown_dish_resolves_via_ingredient(self):
        f = self.svc.lookup(
            "блюдо с курицей", ingredients=["рис", "морковь"], portion_g=150,
        )
        assert f is not None
        assert f.matched_dish == "рис"

    def test_dish_resolves_first_ingredients_ignored(self):
        # If dish name itself resolves, ingredients are not consulted.
        f = self.svc.lookup(
            "борщ", ingredients=["рис"], portion_g=300,
        )
        assert f is not None
        assert f.matched_dish == "борщ"

    def test_no_ingredient_matches_returns_none(self):
        assert self.svc.lookup(
            "неизвестное блюдо", ingredients=["лосось", "авокадо"],
        ) is None

    def test_empty_ingredients_returns_none_for_unknown_dish(self):
        assert self.svc.lookup("неизвестное блюдо", ingredients=[]) is None


# ---------------------------------------------------------------------------
# Portion scaling
# ---------------------------------------------------------------------------


class TestPortionScaling:
    def setup_method(self):
        self.svc = NutritionLookup()

    def test_300g_borscht_totals(self):
        f = self.svc.lookup("борщ", portion_g=300)
        assert f is not None
        # Seed: kcal_per_100g=49 → 300g = 147
        assert f.kcal == 147.0
        # protein_per_100=1.6 → 4.8
        assert f.protein_g == pytest.approx(4.8, rel=1e-2)
        assert f.fat_g == pytest.approx(6.6, rel=1e-2)
        assert f.carbs_g == pytest.approx(20.1, rel=1e-2)

    def test_per_100g_always_present(self):
        f = self.svc.lookup("борщ", portion_g=300)
        assert f is not None
        assert f.kcal_per_100g == 49.0

    def test_no_portion_totals_are_none(self):
        f = self.svc.lookup("борщ")
        assert f is not None
        assert f.kcal is None
        assert f.protein_g is None
        assert f.portion_g is None
        # Per-100g still populated
        assert f.kcal_per_100g == 49.0

    def test_zero_portion_treated_as_unknown(self):
        f = self.svc.lookup("борщ", portion_g=0)
        assert f is not None
        assert f.kcal is None
        assert f.portion_g is None

    def test_negative_portion_treated_as_unknown(self):
        f = self.svc.lookup("борщ", portion_g=-50)
        assert f is not None
        assert f.kcal is None


# ---------------------------------------------------------------------------
# to_dict shape (this is what FoodScan.nutrition stores)
# ---------------------------------------------------------------------------


class TestToDict:
    def test_to_dict_keys(self):
        f = NutritionLookup().lookup("борщ", portion_g=300)
        assert f is not None
        d = f.to_dict()
        assert set(d.keys()) == {
            "matched_dish", "source", "portion_g",
            "kcal_per_100g", "protein_g_per_100g",
            "fat_g_per_100g", "carbs_g_per_100g",
            "kcal", "protein_g", "fat_g", "carbs_g",
        }

    def test_to_dict_serializable(self):
        import json

        f = NutritionLookup().lookup("борщ", portion_g=300)
        assert f is not None
        # Must round-trip JSON for FoodScan.nutrition storage.
        json.dumps(f.to_dict())


# ---------------------------------------------------------------------------
# Custom seed (DI escape hatch for future extensions)
# ---------------------------------------------------------------------------


class TestCustomSeed:
    def test_custom_seed_isolates_from_global(self):
        from nutrition.data.ru_dishes_seed import DishMacros

        custom = {"тестблюдо": DishMacros(100, 5, 5, 5)}
        svc = NutritionLookup(dish_macros=custom, aliases={})
        # Custom hit
        f = svc.lookup("тестблюдо", portion_g=200)
        assert f is not None
        assert f.kcal == 200.0
        # Global hit should NOT resolve when we override
        assert svc.lookup("борщ") is None
