"""DRF-261 — extended NutritionLookup chain (seed → USDA → AI).

Wires the three sources into a single chain. Order matters: cheapest
and most authoritative first.

1. Local seed (Скурихин-Тутельян) — RU dishes the pilot expects.
2. USDA FoodData Central — global reference, free, cached.
3. ``MicronutrientEstimator`` — gpt-4o-mini fallback, tagged
   ``micronutrients_source="ai_estimate"``.

Each layer raises ``USDAUnavailableError`` / ``EstimatorError`` to let
the chain fall through to the next; a clean miss (no result) returns
``None`` from that layer and the chain proceeds.

The existing ``NutritionLookup`` is the public entry point — callers
keep using ``NutritionLookup().lookup(...)``; the chain is plumbed
internally through ``__init__`` injection so tests can stub out USDA
and the estimator without HTTP / OpenAI calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from nutrition.services.nutrition_lookup import (
    NutritionFacts,
    NutritionLookup,
)
from nutrition.services.usda_lookup import USDAUnavailableError
from nutrition.services.micronutrient_estimator import EstimatorError


def _seeded_facts(dish="борщ", source="rospotrebnadzor"):
    return NutritionFacts(
        matched_dish=dish, source="seed_ru", portion_g=200,
        kcal_per_100g=49, protein_g_per_100g=1.6,
        fat_g_per_100g=2.2, carbs_g_per_100g=6.7,
        kcal=98, protein_g=3.2, fat_g=4.4, carbs_g=13.4,
        iron_mg_per_100g=0.6, iron_mg=1.2,
        micronutrients_source=source,
    )


def _usda_facts(dish="apple"):
    return NutritionFacts(
        matched_dish=dish, source="usda", portion_g=100,
        kcal_per_100g=52, protein_g_per_100g=0.26,
        fat_g_per_100g=0.17, carbs_g_per_100g=13.81,
        kcal=52, protein_g=0.26, fat_g=0.17, carbs_g=13.81,
        iron_mg_per_100g=0.12, iron_mg=0.12,
        vitamin_c_mg_per_100g=4.6, vitamin_c_mg=4.6,
        micronutrients_source="usda_full",
    )


def _ai_facts(dish="rare dish"):
    return NutritionFacts(
        matched_dish=dish, source="ai_estimate", portion_g=100,
        kcal_per_100g=180, protein_g_per_100g=8,
        fat_g_per_100g=5, carbs_g_per_100g=20,
        kcal=180, protein_g=8, fat_g=5, carbs_g=20,
        iron_mg_per_100g=1.2, iron_mg=1.2,
        micronutrients_source="ai_estimate",
    )


class TestLookupChainSeedHits:
    """If seed has the dish, USDA + AI never run."""

    def test_seed_hit_skips_usda_and_ai(self):
        usda = MagicMock()
        usda.lookup.return_value = _usda_facts()
        ai = MagicMock()
        ai.estimate.return_value = _ai_facts()

        lookup = NutritionLookup(usda_lookup=usda, ai_estimator=ai)
        result = lookup.lookup("борщ", portion_g=200)

        assert result is not None
        assert result.source == "seed_ru"
        usda.lookup.assert_not_called()
        ai.estimate.assert_not_called()


class TestLookupChainUSDAFallback:
    """Seed miss + USDA hit → returns USDA facts. AI never runs."""

    def test_seed_miss_falls_through_to_usda(self):
        usda = MagicMock()
        usda.lookup.return_value = _usda_facts(dish="apple")
        ai = MagicMock()
        ai.estimate.return_value = _ai_facts()

        # Empty seed → guaranteed seed miss.
        lookup = NutritionLookup(
            dish_macros={}, aliases={},
            usda_lookup=usda, ai_estimator=ai,
        )
        result = lookup.lookup("apple", portion_g=100)

        assert result is not None
        assert result.source == "usda"
        usda.lookup.assert_called_once()
        ai.estimate.assert_not_called()

    def test_usda_unavailable_falls_through_to_ai(self):
        usda = MagicMock()
        usda.lookup.side_effect = USDAUnavailableError("timeout")
        ai = MagicMock()
        ai.estimate.return_value = _ai_facts(dish="apple")

        lookup = NutritionLookup(
            dish_macros={}, aliases={},
            usda_lookup=usda, ai_estimator=ai,
        )
        result = lookup.lookup("apple", portion_g=100)

        assert result is not None
        assert result.micronutrients_source == "ai_estimate"
        ai.estimate.assert_called_once()


class TestLookupChainAIFallback:
    """Seed miss + USDA miss → AI estimator runs."""

    def test_all_misses_call_ai(self):
        usda = MagicMock()
        usda.lookup.return_value = None  # USDA returned no foods.
        ai = MagicMock()
        ai.estimate.return_value = _ai_facts(dish="exotic dish")

        lookup = NutritionLookup(
            dish_macros={}, aliases={},
            usda_lookup=usda, ai_estimator=ai,
        )
        result = lookup.lookup("exotic dish", portion_g=100)

        assert result is not None
        assert result.micronutrients_source == "ai_estimate"
        ai.estimate.assert_called_once()


class TestLookupChainAllMiss:
    """Seed miss + USDA miss + AI error → returns None."""

    def test_returns_none_on_total_miss(self):
        usda = MagicMock()
        usda.lookup.return_value = None
        ai = MagicMock()
        ai.estimate.side_effect = EstimatorError("bad json")

        lookup = NutritionLookup(
            dish_macros={}, aliases={},
            usda_lookup=usda, ai_estimator=ai,
        )
        result = lookup.lookup("xyzzy", portion_g=100)

        assert result is None


class TestLookupChainBackwardsCompat:
    """Existing callers without explicit ``usda_lookup`` / ``ai_estimator``
    must still work — chain falls back to seed-only behaviour gracefully.
    """

    def test_legacy_constructor_still_works(self):
        # No injection — existing call sites must keep working.
        lookup = NutritionLookup()
        result = lookup.lookup("борщ", portion_g=200)

        assert result is not None
        assert result.source == "seed_ru"

    def test_legacy_seed_miss_returns_none_without_chain(self):
        # When USDA + AI are not configured, miss returns None (no
        # implicit network calls).
        lookup = NutritionLookup(dish_macros={}, aliases={})
        result = lookup.lookup("anything", portion_g=100)
        assert result is None
