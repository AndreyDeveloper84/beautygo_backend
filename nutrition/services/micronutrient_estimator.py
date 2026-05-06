"""LLM-based micronutrient estimator (DRF-261).

Last fallback in the Track E lookup chain. When seed misses (dish not
in Скурихин-Тутельян) AND USDA misses (non-USDA-indexed dish or
outage), we ask gpt-4o-mini to estimate per-100g nutrition for the
named dish.

Output is tagged ``micronutrients_source="ai_estimate"`` so the Track
E pattern engine can skip windows where >40% of FoodLog rows came from
this layer — hallucinated micronutrients become bogus deficit signals
that drive cross-domain rules, and "AI thinks borscht has 18 mg iron"
would silently turn into a cosmetology recommendation. The provenance
tag is the firewall.

This module is **never** called for users with eating-disorder flags
— that gate is enforced upstream in the lookup-chain wiring (see
NutritionLookup), not here. Keeping the gate close to user state
avoids leaking ED status into the LLM prompt.

Cost: ~₽0.5 per call at gpt-4o-mini rates. Daily budget alert in
Sentry kicks in above ₽1000/day.
"""
from __future__ import annotations

import json
from typing import Any

from ai.services.llm_client import get_openai_client
from nutrition.services.nutrition_lookup import NutritionFacts


SYSTEM_PROMPT = """Ты — нутрициолог. Оцени состав блюда (среднее значение
на 100 граммов готового блюда). Возвращай только JSON-объект без
комментариев.

Обязательные поля:
- matched_dish (string)
- kcal_per_100g (number, >= 0)
- protein_g_per_100g (number, >= 0)
- fat_g_per_100g (number, >= 0)
- carbs_g_per_100g (number, >= 0)

Опциональные поля (укажи если уверен; null если данных нет):
- vitamin_d_iu_per_100g
- vitamin_b12_mcg_per_100g
- vitamin_c_mg_per_100g
- iron_mg_per_100g
- calcium_mg_per_100g
- magnesium_mg_per_100g
- omega3_g_per_100g
- fiber_g_per_100g

Если блюдо не существует или ты не можешь оценить — верни {"error": "..."}.
Не выдумывай несуществующие блюда."""

_REQUIRED_NUMERIC = (
    "kcal_per_100g",
    "protein_g_per_100g",
    "fat_g_per_100g",
    "carbs_g_per_100g",
)

_OPTIONAL_MICROS = (
    "vitamin_d_iu_per_100g",
    "vitamin_b12_mcg_per_100g",
    "vitamin_c_mg_per_100g",
    "iron_mg_per_100g",
    "calcium_mg_per_100g",
    "magnesium_mg_per_100g",
    "omega3_g_per_100g",
    "fiber_g_per_100g",
)


class EstimatorError(Exception):
    """LLM returned invalid / missing / negative data."""


class MicronutrientEstimator:
    """Pure-Python wrapper around gpt-4o-mini's JSON-mode chat completion."""

    MODEL = "gpt-4o-mini"
    SOURCE_AI = "ai_estimate"

    def estimate(
        self,
        dish_name: str,
        *,
        ingredients: tuple[str, ...] = (),
        portion_g: float | None = None,
    ) -> NutritionFacts:
        """Ask gpt-4o-mini for a micronutrient estimate.

        Raises ``EstimatorError`` when the model returns non-JSON,
        misses required keys, or supplies negative numbers. The lookup
        chain treats this as a hard miss and falls through to None.
        """
        client = get_openai_client()
        user_msg = self._build_user_prompt(dish_name, ingredients)

        response = client.chat.completions.create(
            model=self.MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )

        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise EstimatorError(f"non-JSON response: {exc}") from exc

        if "error" in data:
            raise EstimatorError(f"model declined: {data['error']}")

        self._validate(data)
        return self._build_facts(dish_name, data, portion_g)

    @staticmethod
    def _build_user_prompt(dish: str, ingredients: tuple[str, ...]) -> str:
        if ingredients:
            return f"Блюдо: {dish}\nИнгредиенты: {', '.join(ingredients)}"
        return f"Блюдо: {dish}"

    @staticmethod
    def _validate(data: dict[str, Any]) -> None:
        for key in _REQUIRED_NUMERIC:
            val = data.get(key)
            if val is None:
                raise EstimatorError(f"missing required key: {key}")
            try:
                v = float(val)
            except (TypeError, ValueError) as exc:
                raise EstimatorError(f"non-numeric {key}: {val!r}") from exc
            if v < 0:
                raise EstimatorError(f"negative {key}: {v}")

    @staticmethod
    def _build_facts(
        dish_name: str, data: dict[str, Any], portion_g: float | None,
    ) -> NutritionFacts:
        ratio = (portion_g / 100.0) if (portion_g and portion_g > 0) else None

        def _scale(per_100g):
            if per_100g is None or ratio is None:
                return None
            return round(float(per_100g) * ratio, 2)

        kcal_p100 = float(data["kcal_per_100g"])
        protein_p100 = float(data["protein_g_per_100g"])
        fat_p100 = float(data["fat_g_per_100g"])
        carbs_p100 = float(data["carbs_g_per_100g"])

        # Optional micros — pass through, normalize None.
        micros: dict[str, float | None] = {
            key: (None if data.get(key) is None else float(data[key]))
            for key in _OPTIONAL_MICROS
        }

        return NutritionFacts(
            matched_dish=data.get("matched_dish") or dish_name,
            source="ai_estimate",
            portion_g=portion_g if (portion_g and portion_g > 0) else None,
            kcal_per_100g=kcal_p100,
            protein_g_per_100g=protein_p100,
            fat_g_per_100g=fat_p100,
            carbs_g_per_100g=carbs_p100,
            kcal=_scale(kcal_p100),
            protein_g=_scale(protein_p100),
            fat_g=_scale(fat_p100),
            carbs_g=_scale(carbs_p100),
            vitamin_d_iu_per_100g=micros["vitamin_d_iu_per_100g"],
            vitamin_b12_mcg_per_100g=micros["vitamin_b12_mcg_per_100g"],
            vitamin_c_mg_per_100g=micros["vitamin_c_mg_per_100g"],
            iron_mg_per_100g=micros["iron_mg_per_100g"],
            calcium_mg_per_100g=micros["calcium_mg_per_100g"],
            magnesium_mg_per_100g=micros["magnesium_mg_per_100g"],
            omega3_g_per_100g=micros["omega3_g_per_100g"],
            fiber_g_per_100g=micros["fiber_g_per_100g"],
            vitamin_d_iu=_scale(micros["vitamin_d_iu_per_100g"]),
            vitamin_b12_mcg=_scale(micros["vitamin_b12_mcg_per_100g"]),
            vitamin_c_mg=_scale(micros["vitamin_c_mg_per_100g"]),
            iron_mg=_scale(micros["iron_mg_per_100g"]),
            calcium_mg=_scale(micros["calcium_mg_per_100g"]),
            magnesium_mg=_scale(micros["magnesium_mg_per_100g"]),
            omega3_g=_scale(micros["omega3_g_per_100g"]),
            fiber_g=_scale(micros["fiber_g_per_100g"]),
            micronutrients_source="ai_estimate",
        )
