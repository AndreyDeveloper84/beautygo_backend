"""DRF-261 — USDA FoodData Central lookup + cache.

Track E pattern engine needs micronutrient values for any dish a user
logs. The Скурихин-Тутельян seed covers ~80% of RU home-cooked meals
(борщ, оливье, гречка), but anything else — a USA-style sandwich, an
unfamiliar protein, a packaged product — must come from USDA's free
FoodData Central API. Cache results in ``USDAFoodCache`` so a popular
dish doesn't repeatedly hit the network.

Contract:
- ``USDALookup.lookup(dish_name)`` returns a ``NutritionFacts`` dataclass
  populated with macros + micronutrients, or ``None`` on miss.
- Network failures (timeout, 5xx) raise ``USDAUnavailableError`` so the
  caller can fall through to the AI estimate layer.
- Successful lookups write to ``USDAFoodCache`` keyed by the normalized
  dish name; subsequent lookups read from cache without HTTP.
- ``micronutrients_source`` is set to ``"usda_full"`` if USDA returned
  values for ≥6 of 8 micronutrients, ``"usda_partial"`` otherwise.

USDA API responses are HUGE (foods endpoint returns 100+ nutrient rows
per food) — the parser keeps only the eight Track E micronutrients
(plus macros) and discards the rest.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from nutrition.models import USDAFoodCache
from nutrition.services.usda_lookup import (
    USDALookup,
    USDAUnavailableError,
)


# Fixture: a slice of a real USDA FoodData Central /v1/foods/search response.
# Real responses have 100+ nutrient rows per food; we keep just enough rows
# to exercise the parser's macro + micronutrient extraction.
SAMPLE_USDA_RESPONSE = {
    "foods": [
        {
            "fdcId": 168929,
            "description": "Apple, raw, with skin",
            "foodNutrients": [
                {"nutrientId": 1008, "nutrientName": "Energy", "value": 52.0,
                 "unitName": "KCAL"},
                {"nutrientId": 1003, "nutrientName": "Protein", "value": 0.26,
                 "unitName": "G"},
                {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "value": 0.17,
                 "unitName": "G"},
                {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference",
                 "value": 13.81, "unitName": "G"},
                # Micronutrients (Track E targets)
                {"nutrientId": 1110, "nutrientName": "Vitamin D (D2 + D3), International Units",
                 "value": 0.0, "unitName": "IU"},
                {"nutrientId": 1178, "nutrientName": "Vitamin B-12", "value": 0.0,
                 "unitName": "UG"},
                {"nutrientId": 1162, "nutrientName": "Vitamin C, total ascorbic acid",
                 "value": 4.6, "unitName": "MG"},
                {"nutrientId": 1089, "nutrientName": "Iron, Fe", "value": 0.12,
                 "unitName": "MG"},
                {"nutrientId": 1087, "nutrientName": "Calcium, Ca", "value": 6.0,
                 "unitName": "MG"},
                {"nutrientId": 1090, "nutrientName": "Magnesium, Mg", "value": 5.0,
                 "unitName": "MG"},
                # No omega-3 row in apple — parser must leave None.
                {"nutrientId": 1079, "nutrientName": "Fiber, total dietary",
                 "value": 2.4, "unitName": "G"},
            ],
        },
    ],
}


@pytest.fixture
def mock_httpx_post():
    """Mock httpx.Client.post — returns SAMPLE_USDA_RESPONSE."""
    with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.post.return_value = httpx.Response(
            status_code=200,
            json=SAMPLE_USDA_RESPONSE,
            request=httpx.Request("POST", "https://api.nal.usda.gov/fdc/v1/foods/search"),
        )
        yield instance


@pytest.mark.django_db
class TestUSDALookupHappyPath:
    """USDA returns 200 + a foods array → we parse + cache + return facts."""

    def test_returns_nutrition_facts_with_macros(self, mock_httpx_post, settings):
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()

        facts = lookup.lookup("apple", portion_g=100)

        assert facts is not None
        assert facts.matched_dish.lower().startswith("apple")
        assert facts.kcal_per_100g == 52.0
        assert facts.protein_g_per_100g == 0.26
        assert facts.carbs_g_per_100g == 13.81

    def test_returns_micronutrients_with_unit_conversion(
        self, mock_httpx_post, settings,
    ):
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()

        facts = lookup.lookup("apple", portion_g=100)

        assert facts.vitamin_c_mg_per_100g == 4.6
        assert facts.iron_mg_per_100g == 0.12
        assert facts.calcium_mg_per_100g == 6.0
        assert facts.magnesium_mg_per_100g == 5.0
        assert facts.fiber_g_per_100g == 2.4
        # Apple has no B12 / D / omega-3 — None when USDA didn't list them.
        # (Apple's B12 row is 0 → recorded as 0.0, not None — distinguish.)
        assert facts.vitamin_d_iu_per_100g == 0.0
        assert facts.vitamin_b12_mcg_per_100g == 0.0
        assert facts.omega3_g_per_100g is None  # truly absent → None

    def test_source_is_usda_full_when_six_or_more_micros(
        self, mock_httpx_post, settings,
    ):
        # Apple sample has 7/8 micros (no omega-3). 7 >= 6 → usda_full.
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()
        facts = lookup.lookup("apple", portion_g=100)
        assert facts.micronutrients_source == "usda_full"

    def test_source_is_usda_partial_when_less_than_six(self, settings):
        # Build a partial response with only 3 micros.
        partial = {
            "foods": [{
                "fdcId": 1, "description": "Test",
                "foodNutrients": [
                    {"nutrientId": 1008, "nutrientName": "Energy",
                     "value": 100, "unitName": "KCAL"},
                    {"nutrientId": 1003, "nutrientName": "Protein",
                     "value": 5, "unitName": "G"},
                    {"nutrientId": 1004, "nutrientName": "Total lipid (fat)",
                     "value": 2, "unitName": "G"},
                    {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference",
                     "value": 15, "unitName": "G"},
                    {"nutrientId": 1089, "nutrientName": "Iron, Fe",
                     "value": 2.0, "unitName": "MG"},
                    {"nutrientId": 1087, "nutrientName": "Calcium, Ca",
                     "value": 50, "unitName": "MG"},
                    {"nutrientId": 1162, "nutrientName": "Vitamin C, total ascorbic acid",
                     "value": 8.0, "unitName": "MG"},
                ],
            }],
        }
        settings.USDA_API_KEY = "test-key"
        with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = httpx.Response(
                200, json=partial,
                request=httpx.Request("POST", "https://x.example"),
            )
            lookup = USDALookup()
            facts = lookup.lookup("test_dish", portion_g=100)

        assert facts.micronutrients_source == "usda_partial"


@pytest.mark.django_db
class TestUSDALookupCache:
    """Successful lookups write to USDAFoodCache; subsequent reads skip HTTP."""

    def test_first_lookup_writes_cache_row(self, mock_httpx_post, settings):
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()
        lookup.lookup("apple", portion_g=100)

        assert USDAFoodCache.objects.count() == 1
        row = USDAFoodCache.objects.get()
        assert row.fdc_id == 168929
        assert "Apple" in row.description

    def test_second_lookup_reads_cache_without_http(
        self, mock_httpx_post, settings,
    ):
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()

        lookup.lookup("apple", portion_g=100)
        # Second call should not POST again.
        lookup.lookup("apple", portion_g=100)

        # post() called exactly once across the two lookups.
        assert mock_httpx_post.post.call_count == 1

    def test_cache_match_is_normalized(self, mock_httpx_post, settings):
        # "Apple" / "  apple " / "APPLE" should all hit the same cache row.
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()
        lookup.lookup("Apple", portion_g=100)
        lookup.lookup("  apple ", portion_g=100)
        lookup.lookup("APPLE", portion_g=100)

        assert mock_httpx_post.post.call_count == 1


@pytest.mark.django_db
class TestUSDALookupMisses:
    """Empty foods array → None, no cache write."""

    def test_no_foods_returns_none(self, settings):
        empty = {"foods": []}
        settings.USDA_API_KEY = "test-key"
        with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = httpx.Response(
                200, json=empty,
                request=httpx.Request("POST", "https://x"),
            )
            lookup = USDALookup()
            result = lookup.lookup("nonexistent_dish_xyz", portion_g=100)

        assert result is None
        assert USDAFoodCache.objects.count() == 0


@pytest.mark.django_db
class TestUSDALookupErrors:
    """Network errors / 5xx raise USDAUnavailableError so caller can fallback."""

    def test_timeout_raises_unavailable(self, settings):
        settings.USDA_API_KEY = "test-key"
        with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.side_effect = httpx.TimeoutException("timeout")
            lookup = USDALookup()
            with pytest.raises(USDAUnavailableError):
                lookup.lookup("apple", portion_g=100)

    def test_5xx_raises_unavailable(self, settings):
        settings.USDA_API_KEY = "test-key"
        with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = httpx.Response(
                503, text="service down",
                request=httpx.Request("POST", "https://x"),
            )
            lookup = USDALookup()
            with pytest.raises(USDAUnavailableError):
                lookup.lookup("apple", portion_g=100)

    def test_4xx_returns_none(self, settings):
        # 4xx (e.g. 400 bad query) is a miss, not an outage. Don't raise.
        settings.USDA_API_KEY = "test-key"
        with patch("nutrition.services.usda_lookup.httpx.Client") as MockClient:
            instance = MockClient.return_value.__enter__.return_value
            instance.post.return_value = httpx.Response(
                400, json={"error": "bad query"},
                request=httpx.Request("POST", "https://x"),
            )
            lookup = USDALookup()
            assert lookup.lookup("apple", portion_g=100) is None

    def test_no_api_key_raises_at_construction(self, settings):
        # Loud failure beats silent fallback when the key isn't configured.
        settings.USDA_API_KEY = ""
        with pytest.raises(ValueError, match="USDA_API_KEY"):
            USDALookup()


@pytest.mark.django_db
class TestUSDALookupPortionScaling:
    """USDA returns per-100g values; consumers want totals at portion_g."""

    def test_portion_scales_micronutrients(self, mock_httpx_post, settings):
        settings.USDA_API_KEY = "test-key"
        lookup = USDALookup()
        # 200g portion → values double.
        facts = lookup.lookup("apple", portion_g=200)

        assert facts.iron_mg == round(0.12 * 2, 2)
        assert facts.vitamin_c_mg == round(4.6 * 2, 2)
        assert facts.calcium_mg == round(6.0 * 2, 2)
