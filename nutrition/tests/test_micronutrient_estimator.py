"""DRF-261 — LLM-based micronutrient estimator (gpt-4o-mini fallback).

When seed misses (dish not in Скурихин-Тутельян) AND USDA misses
(non-USDA-indexed RU dish, or USDA outage), the last fallback is to
ask gpt-4o-mini to estimate macros + micronutrients per 100g for the
named dish. The result is tagged ``micronutrients_source="ai_estimate"``
so the Track E pattern engine can skip windows where >40% of FoodLog
rows came from this path (low-confidence data → bogus deficit signals).

Contract:
- ``MicronutrientEstimator.estimate(dish_name, ingredients=())`` returns
  a ``NutritionFacts`` populated with macros + micronutrients (per 100g
  + per portion if portion_g supplied) and ``micronutrients_source =
  "ai_estimate"``.
- LLM call uses the existing ``ai.services.llm_client.get_openai_client``
  proxy plumbing (no new vendor) and the ``gpt-4o-mini`` model.
- The prompt forces a strict JSON schema; non-JSON or validation-failed
  responses raise ``EstimatorError`` so the caller can surface a
  ``DishNotRecognizedError`` to the user.
- Eating-disorder mode never uses this layer (caller's responsibility,
  documented in module docstring).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from nutrition.services.micronutrient_estimator import (
    EstimatorError,
    MicronutrientEstimator,
)


SAMPLE_LLM_JSON = {
    "matched_dish": "Salade niçoise",
    "kcal_per_100g": 145,
    "protein_g_per_100g": 9.5,
    "fat_g_per_100g": 8.5,
    "carbs_g_per_100g": 7.0,
    "vitamin_d_iu_per_100g": 60.0,
    "vitamin_b12_mcg_per_100g": 0.6,
    "vitamin_c_mg_per_100g": 12.0,
    "iron_mg_per_100g": 1.4,
    "calcium_mg_per_100g": 35.0,
    "magnesium_mg_per_100g": 22.0,
    "omega3_g_per_100g": 0.4,
    "fiber_g_per_100g": 2.5,
}


def _mock_chat_completion(content_dict):
    """Build a minimal OpenAI ChatCompletion-shaped mock."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps(content_dict)
    return mock_response


class TestEstimatorHappyPath:
    def test_returns_nutrition_facts_with_macros(self):
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                SAMPLE_LLM_JSON,
            )
            estimator = MicronutrientEstimator()
            facts = estimator.estimate("Salade niçoise", portion_g=200)

        assert facts.matched_dish == "Salade niçoise"
        assert facts.kcal_per_100g == 145
        assert facts.protein_g_per_100g == 9.5
        # Per-portion totals scaled.
        assert facts.kcal == 290
        assert facts.protein_g == 19.0

    def test_returns_micronutrients_with_ai_estimate_source(self):
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                SAMPLE_LLM_JSON,
            )
            estimator = MicronutrientEstimator()
            facts = estimator.estimate("Salade niçoise", portion_g=100)

        assert facts.iron_mg_per_100g == 1.4
        assert facts.vitamin_c_mg_per_100g == 12.0
        assert facts.fiber_g_per_100g == 2.5
        # Provenance tag — Track E pattern engine reads this.
        assert facts.micronutrients_source == "ai_estimate"

    def test_uses_gpt_4o_mini_model(self):
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                SAMPLE_LLM_JSON,
            )
            estimator = MicronutrientEstimator()
            estimator.estimate("anything", portion_g=100)

            args, kwargs = client.chat.completions.create.call_args
            assert kwargs.get("model") == "gpt-4o-mini"


class TestEstimatorErrors:
    def test_non_json_response_raises(self):
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "I cannot estimate this dish."
            client.chat.completions.create.return_value = mock_resp

            estimator = MicronutrientEstimator()
            with pytest.raises(EstimatorError):
                estimator.estimate("xyzzy", portion_g=100)

    def test_missing_required_keys_raises(self):
        # Validation: kcal_per_100g must be present and numeric.
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                {"matched_dish": "x"},  # everything else missing
            )
            estimator = MicronutrientEstimator()
            with pytest.raises(EstimatorError):
                estimator.estimate("x", portion_g=100)

    def test_negative_kcal_rejected(self):
        bogus = dict(SAMPLE_LLM_JSON, kcal_per_100g=-10)
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                bogus,
            )
            estimator = MicronutrientEstimator()
            with pytest.raises(EstimatorError):
                estimator.estimate("anything", portion_g=100)


class TestEstimatorPromptShape:
    def test_prompt_includes_dish_name(self):
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                SAMPLE_LLM_JSON,
            )
            estimator = MicronutrientEstimator()
            estimator.estimate("борщ украинский", portion_g=100)

            args, kwargs = client.chat.completions.create.call_args
            messages = kwargs.get("messages", [])
            user_msg = next(m for m in messages if m["role"] == "user")
            assert "борщ украинский" in user_msg["content"]

    def test_prompt_requests_json_object(self):
        # Force JSON mode via response_format so we don't have to parse
        # natural-language replies.
        with patch(
            "nutrition.services.micronutrient_estimator.get_openai_client",
        ) as mock_factory:
            client = mock_factory.return_value
            client.chat.completions.create.return_value = _mock_chat_completion(
                SAMPLE_LLM_JSON,
            )
            estimator = MicronutrientEstimator()
            estimator.estimate("x", portion_g=100)

            args, kwargs = client.chat.completions.create.call_args
            assert kwargs.get("response_format") == {"type": "json_object"}
