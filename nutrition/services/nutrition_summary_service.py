"""Nutrition daily summary aggregation.

Spec: Notion API Spec v2.0 §FOOD SCANNER+NUTRITION
      GET /nutrition/summary?date=YYYY-MM-DD → NutritionSummaryResponse:

    {
        date, calories_total, calories_goal, protein_g, fat_g, carbs_g,
        water_ml, water_goal_ml, entries[], vitamin_deficits
    }

Slice 3c implements the food side of this. Two stubs are intentional
and call out the next slices:

- ``water_ml`` / ``water_goal_ml``: stubs (0 / settings default 2000ml)
  until Slice 4 ships ``WaterLog`` and the +water/-water endpoints.
- ``vitamin_deficits``: empty dict until Slice 3a' brings OFF/USDA
  vitamin data into the seed lookup.

Day boundaries:
- Date param is interpreted as a **UTC** calendar day for MVP. The
  pilot is single-timezone (Penza, MSK = UTC+3) and a 3-hour window
  drift is tolerable for a "what did I eat today" diary. When the
  user-tz field lands on UserPersonalContext, switch to that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from django.conf import settings
from django.db.models import Sum

from nutrition.models import FoodLog


@dataclass(frozen=True)
class SummaryTotals:
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float


@dataclass(frozen=True)
class NutritionSummary:
    """Carrier shape — serialiser maps this to the spec response."""
    date: date
    totals: SummaryTotals
    calories_goal: int
    water_ml: int
    water_goal_ml: int
    entries: list[FoodLog]
    vitamin_deficits: dict[str, float]


class NutritionSummaryService:
    """Aggregates FoodLog rows for one user on one calendar day."""

    def summary(self, *, user_id: int, day: date) -> NutritionSummary:
        start = datetime.combine(day, time.min, tzinfo=timezone.utc)
        end = datetime.combine(day, time.max, tzinfo=timezone.utc)

        qs = (
            FoodLog.objects
            .filter(user_id=user_id, logged_at__gte=start, logged_at__lte=end)
            .order_by("logged_at")
        )

        # One DB hit for totals, one for entries — entries query reuses
        # the (user, -logged_at) index defined on FoodLog.
        agg = qs.aggregate(
            calories=Sum("calories"),
            protein_g=Sum("protein_g"),
            fat_g=Sum("fat_g"),
            carbs_g=Sum("carbs_g"),
        )
        totals = SummaryTotals(
            calories=_round1(agg["calories"]),
            protein_g=_round1(agg["protein_g"]),
            fat_g=_round1(agg["fat_g"]),
            carbs_g=_round1(agg["carbs_g"]),
        )

        return NutritionSummary(
            date=day,
            totals=totals,
            calories_goal=settings.NUTRITION_DEFAULT_CALORIES_GOAL,
            # Slice 4 will replace these stubs with WaterLog aggregates.
            water_ml=0,
            water_goal_ml=settings.NUTRITION_DEFAULT_WATER_GOAL_ML,
            entries=list(qs),
            # Slice 3a' (OFF/USDA fallback) will populate when the seed
            # carries vitamin data; the per-100g seed dictionary doesn't
            # currently include vitamin breakdowns.
            vitamin_deficits={},
        )


def _round1(value: float | None) -> float:
    if value is None:
        return 0.0
    return round(float(value), 1)
