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


@dataclass(frozen=True)
class ProgressiveSummary:
    """DRF-266 — 14d/28d retention summary.

    Distinct shape from ``NutritionSummary``: trends over time instead
    of a single-day snapshot. The bot serialiser switches between the
    two on the presence/absence of ``period`` in the query string.
    """

    period: int                      # 7 / 14 / 28
    unlocked: bool                   # commitment_days >= period
    commitment_days: int
    trends: dict                     # per-7-day-bucket averages, empty when locked
    habits: dict                     # streaks + counters
    goal_progress: dict | None       # only for goal in {lose, gain}


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


# ---------------------------------------------------------------------------
# DRF-266 — progressive summary
# ---------------------------------------------------------------------------


def _add_progressive_method():
    """Bolt the progressive method onto NutritionSummaryService.

    Defined as a free function then attached to keep the original class
    body untouched — easier to review the diff.
    """
    from nutrition.services.commitment_service import get_commitment_days

    def progressive(
        self, *, user, period: int,
    ) -> ProgressiveSummary:
        """Return per-week bucket trends over the trailing ``period`` days.

        ``period`` ∈ {7, 14, 28}. Locked (unlocked=False) when commitment
        is below the threshold — bot then renders a teaser «ещё N дней».
        """
        if period not in (7, 14, 28):
            raise ValueError(f"period must be 7/14/28, got {period}")

        commitment = get_commitment_days(user)
        # period=7 is the default weekly view — always unlocked, no
        # commitment gate. Track E retention play (14d/28d) only kicks
        # in for the longer windows where trend data becomes meaningful.
        unlocked = period == 7 or commitment >= period

        if not unlocked:
            return ProgressiveSummary(
                period=period, unlocked=False,
                commitment_days=commitment,
                trends={}, habits={}, goal_progress=None,
            )

        # Aggregate per-week buckets. Each bucket = 7 calendar days
        # ending today, today-7, today-14, ... up to ``period``.
        today = datetime.now(timezone.utc).date()
        weeks = period // 7
        bucket_kcal: list[float] = []
        bucket_protein: list[float] = []
        bucket_omega3: list[float] = []
        bucket_vitamin_d: list[float] = []

        # Pull profile RDA once outside the loop. DRF-266 LB-4 fix:
        # falling back to 1 IU produced 20000% percentages for any
        # user whose profile predates DRF-265 (where daily_vitamin_d_iu
        # is set). Use settings default 600 IU instead — matches USDA
        # adult RDA. After DRF-265 lands, profile-side value takes over
        # automatically because we read it first.
        profile = getattr(user, "nutrition_profile", None)
        from django.conf import settings as dj_settings
        rda_vitamin_d = (
            getattr(profile, "daily_vitamin_d_iu", 0)
            or getattr(dj_settings, "NUTRITION_DEFAULT_VITAMIN_D_IU", 600)
        ) if profile else getattr(
            dj_settings, "NUTRITION_DEFAULT_VITAMIN_D_IU", 600,
        )

        for w in range(weeks):
            week_end = today - timedelta(days=w * 7)
            week_start = week_end - timedelta(days=6)
            # Bound the bucket with explicit UTC datetimes rather than
            # ``logged_at__date`` lookups. ``__date`` truncates in the
            # connection timezone (settings.TIME_ZONE = Europe/Moscow),
            # while ``today`` above is a UTC date — so when CI/prod runs
            # past 21:00 UTC (00:00+ MSK) every log's date shifted +1,
            # pushing the newest day out of the window and leaving the
            # oldest 7-day bucket with only 6 days → 6000/7 = 857.1.
            # Pinning to UTC matches summary()/weekly_deficits() and the
            # UTC ``today`` anchor, making buckets timezone-stable.
            start_dt = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
            end_dt = datetime.combine(week_end, time.max, tzinfo=timezone.utc)
            qs = FoodLog.objects.filter(
                user=user,
                logged_at__gte=start_dt,
                logged_at__lte=end_dt,
            )
            agg = qs.aggregate(
                kcal=Sum("calories"),
                protein=Sum("protein_g"),
                omega3=Sum("omega3_g"),
                vitamin_d=Sum("vitamin_d_iu"),
            )
            # Per-day average — divide weekly total by 7.
            bucket_kcal.append(_round1((agg["kcal"] or 0) / 7))
            bucket_protein.append(_round1((agg["protein"] or 0) / 7))
            bucket_omega3.append(_round1((agg["omega3"] or 0) / 7))
            # Vitamin D as % of RDA (already daily-averaged).
            avg_vd = (agg["vitamin_d"] or 0) / 7
            bucket_vitamin_d.append(round((avg_vd / rda_vitamin_d) * 100, 1))

        # Order: oldest week first, newest last (matches mobile chart UX).
        trends = {
            "calories_avg_per_week": list(reversed(bucket_kcal)),
            "protein_g_avg_per_week": list(reversed(bucket_protein)),
            "omega3_g_avg_per_week": list(reversed(bucket_omega3)),
            "vitamin_d_pct_rda_per_week": list(reversed(bucket_vitamin_d)),
        }

        habits = _compute_habits(user, period)
        goal_progress = _compute_goal_progress(profile)

        return ProgressiveSummary(
            period=period, unlocked=True,
            commitment_days=commitment,
            trends=trends, habits=habits, goal_progress=goal_progress,
        )

    NutritionSummaryService.progressive = progressive


def _compute_habits(user, period: int) -> dict:
    """Streak counters used by mobile UX («21 день подряд завтрак»).

    DRF-266 LB-3 fix: late_dinner_count was using `logged_at__hour__gte=21`
    in **UTC** hours. Penza pilot is MSK (UTC+3) — 22:00 MSK is 19:00
    UTC, so the original filter never matched real late dinners. Now
    we count any dinner whose logged_at, viewed in user's local
    timezone (defaulting to MSK for the Penza pilot), is past 21:00.

    When UserPersonalContext.timezone lands, swap MSK_OFFSET for the
    user's stored TZ. Also sync this helper with pattern_detection_service
    where late_dinner uses the same threshold.
    """
    today = datetime.now(timezone.utc).date()

    # DRF-266 LB-3 fix + M2 review fix: collapse N+1 EXISTS queries
    # into a single distinct-days aggregate.
    breakfast_days = set(
        FoodLog.objects
        .filter(
            user=user, meal_type="breakfast",
            logged_at__date__gte=today - timedelta(days=period),
        )
        .annotate(day=TruncDate("logged_at", tzinfo=timezone.utc))
        .values_list("day", flat=True)
        .distinct()
    )
    breakfast_streak = 0
    cursor = today
    while cursor in breakfast_days:
        breakfast_streak += 1
        cursor -= timedelta(days=1)
        if (today - cursor).days >= period:
            break

    # LB-3: pilot timezone is MSK (UTC+3). 21:00 local = 18:00 UTC.
    # When UserPersonalContext.timezone lands, switch to per-user.
    LATE_DINNER_LOCAL_HOUR = 21
    PILOT_TZ_OFFSET_HOURS = 3  # MSK
    late_dinner_utc_hour = LATE_DINNER_LOCAL_HOUR - PILOT_TZ_OFFSET_HOURS
    late_dinner_count = FoodLog.objects.filter(
        user=user, meal_type="dinner",
        logged_at__date__gte=today - timedelta(days=period - 1),
        logged_at__hour__gte=late_dinner_utc_hour,
    ).count()

    return {
        "breakfast_logged_streak_days": breakfast_streak,
        "late_dinner_count_in_period": late_dinner_count,
    }


def _compute_goal_progress(profile) -> dict | None:
    """Only goal=lose/gain return a progress block. tone/maintain → None."""
    if profile is None or profile.goal not in ("lose", "gain"):
        return None
    return {
        "type": "weight_loss" if profile.goal == "lose" else "weight_gain",
        "current_kcal_target": profile.daily_kcal,
        "current_protein_target": profile.daily_protein_g,
    }


_add_progressive_method()
