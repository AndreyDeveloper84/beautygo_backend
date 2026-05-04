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
from datetime import date, datetime, time, timedelta, timezone

from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import TruncDate

from nutrition.models import FoodLog
from nutrition.services.water_service import WaterService


@dataclass(frozen=True)
class SummaryTotals:
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float


@dataclass(frozen=True)
class WeeklyDeficits:
    """Aggregate signals over the trailing N days for cross-domain hints (DRF-248).

    Used by InternalDeficitsView → AIConcierge prompt enrichment. Designed to
    stay agnostic of vitamin data (vitamin lookups not yet wired — see
    docs/FOOD_SCANNER_DECISION.md). Protein is the only signal we can compute
    reliably from current FoodLog rows.

    Fields:
        days_observed: number of distinct calendar days (UTC) with ≥1 FoodLog
            in the window. 0 means the user hasn't logged anything — caller
            should NOT inject a hint (no signal).
        protein_avg_pct_goal: average of (daily_protein / goal) across
            observed days. ``None`` when days_observed == 0.
        protein_low_streak_days: count of trailing consecutive days
            (ending at the window's last day) where daily protein was
            below ``settings.FOOD_DEFICIT_PROTEIN_THRESHOLD_PCT`` of goal.
            Days with no logs break the streak.
    """

    days_observed: int
    protein_avg_pct_goal: float | None
    protein_low_streak_days: int


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
    # DRF-303 §4.2 — present only when the caller asked with_comment=true.
    # ``None`` distinguishes "not requested" from "requested but empty"
    # downstream renderers.
    ai_comment: str | None = None


class NutritionSummaryService:
    """Aggregates FoodLog rows for one user on one calendar day."""

    def __init__(self, water_service: WaterService | None = None) -> None:
        self._water_service = water_service or WaterService()

    def summary(
        self, *, user_id: int, day: date, with_comment: bool = False,
    ) -> NutritionSummary:
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

        # Slice 4: water aggregate now lives — drops the stub.
        water = self._water_service.aggregate_for_day(user_id, day)
        entries = list(qs)
        calories_goal = settings.NUTRITION_DEFAULT_CALORIES_GOAL

        ai_comment: str | None = None
        if with_comment:
            # Local import — keeps the LLM client out of every summary
            # request and avoids import cycles with the profile module.
            from nutrition.services.ai_comment_service import (
                AICommentService,
                SummaryFacts,
            )
            ai_comment = AICommentService().comment_for(
                user_id=user_id,
                day=day,
                facts=SummaryFacts(
                    calories_total=totals.calories,
                    calories_goal=calories_goal,
                    protein_g=totals.protein_g,
                    fat_g=totals.fat_g,
                    carbs_g=totals.carbs_g,
                    water_ml=water.water_ml,
                    water_goal_ml=water.water_goal_ml,
                    entries_count=len(entries),
                ),
            )

        return NutritionSummary(
            date=day,
            totals=totals,
            calories_goal=calories_goal,
            water_ml=water.water_ml,
            water_goal_ml=water.water_goal_ml,
            entries=entries,
            # Slice 3a' (OFF/USDA fallback) will populate when the seed
            # carries vitamin data; the per-100g seed dictionary doesn't
            # currently include vitamin breakdowns.
            vitamin_deficits={},
            ai_comment=ai_comment,
        )

    def weekly_deficits(self, *, user_id, days: int = 7) -> WeeklyDeficits:
        """Compute trailing-N-day deficit signals for cross-domain bridge (DRF-248).

        Window is ``days`` UTC calendar days ending today. Pure DB aggregation
        — single query, indexed scan on FoodLog.(user, -logged_at).
        """
        today = datetime.now(timezone.utc).date()
        window_start = today - timedelta(days=days - 1)
        start_dt = datetime.combine(window_start, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(today, time.max, tzinfo=timezone.utc)

        per_day = (
            FoodLog.objects
            .filter(user_id=user_id, logged_at__gte=start_dt, logged_at__lte=end_dt)
            .annotate(day=TruncDate("logged_at", tzinfo=timezone.utc))
            .values("day")
            .annotate(protein_total=Sum("protein_g"))
            .order_by("day")
        )
        per_day_list = list(per_day)
        days_observed = len(per_day_list)
        if days_observed == 0:
            return WeeklyDeficits(
                days_observed=0,
                protein_avg_pct_goal=None,
                protein_low_streak_days=0,
            )

        goal = float(settings.NUTRITION_DEFAULT_PROTEIN_GOAL_G or 0.0)
        threshold_pct = float(settings.FOOD_DEFICIT_PROTEIN_THRESHOLD_PCT or 0.0)
        if goal <= 0:
            return WeeklyDeficits(
                days_observed=days_observed,
                protein_avg_pct_goal=None,
                protein_low_streak_days=0,
            )

        protein_pcts: dict[date, float] = {
            row["day"]: float(row["protein_total"] or 0.0) / goal
            for row in per_day_list
        }
        avg_pct = sum(protein_pcts.values()) / len(protein_pcts)

        # Streak: walk backward from today; break on first day that is
        # either missing (no logs that day) or above threshold.
        streak = 0
        cursor = today
        while cursor >= window_start:
            pct = protein_pcts.get(cursor)
            if pct is None or pct >= threshold_pct:
                break
            streak += 1
            cursor = cursor - timedelta(days=1)

        return WeeklyDeficits(
            days_observed=days_observed,
            protein_avg_pct_goal=round(avg_pct, 3),
            protein_low_streak_days=streak,
        )


def _round1(value: float | None) -> float:
    if value is None:
        return 0.0
    return round(float(value), 1)
