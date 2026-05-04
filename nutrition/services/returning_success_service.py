"""Returning-success insight detector (DRF-305).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §3.2.

Detects users who fell off (failure streak) and have come back (recovery
streak) — the bot uses this to fire a `returning_success` nudge that
acknowledges effort without piling on numbers.

Trigger:
- 3+ consecutive days with kcal < 60% of ``daily_kcal`` goal
- IMMEDIATELY followed by 2+ consecutive days with kcal in [80%, 110%]
- ``recovery`` streak ends today (most recent observation)

Window: last 21 days. Days with no FoodLog rows are ``none`` and break
both streaks — we don't infer behaviour from absence of data.

Eating-disorder mode: returns ``{detected: false}`` unconditionally
(spec §3.2 + §10) — never reinforce numeric improvement for ED users.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_tz

from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.db.models.functions import TruncDate

from nutrition.models import FoodLog, NutritionProfile

logger = logging.getLogger(__name__)


WINDOW_DAYS = 21
FAILURE_THRESHOLD_PCT = 0.60
RECOVERY_LO_PCT = 0.80
RECOVERY_HI_PCT = 1.10
MIN_FAILURE_STREAK = 3
MIN_RECOVERY_STREAK = 2

CACHE_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class ReturningSuccessInsight:
    detected: bool
    failure_streak_days: int = 0
    recovery_days: int = 0
    since_recovery_started: str | None = None


def detect_returning_success(*, user_id: int, force: bool = False) -> ReturningSuccessInsight:
    cache_key = f"nutrition.returning_success:{user_id}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    profile = NutritionProfile.objects.filter(user_id=user_id).first()
    flags = (profile.health_flags or {}) if profile else {}
    if flags.get("eating_disorder"):
        out = ReturningSuccessInsight(detected=False)
        cache.set(cache_key, out, CACHE_TTL_SECONDS)
        return out

    goal = float(_resolve_goal(profile))
    if goal <= 0:
        out = ReturningSuccessInsight(detected=False)
        cache.set(cache_key, out, CACHE_TTL_SECONDS)
        return out

    today = datetime.now(dt_tz.utc).date()
    window_start = today - timedelta(days=WINDOW_DAYS - 1)
    per_day_kcal = _per_day_kcal(user_id, window_start, today)

    recovery_days = _trailing_recovery_streak(per_day_kcal, today, goal)
    if recovery_days < MIN_RECOVERY_STREAK:
        out = ReturningSuccessInsight(detected=False, recovery_days=recovery_days)
        cache.set(cache_key, out, CACHE_TTL_SECONDS)
        return out

    failure_end = today - timedelta(days=recovery_days)
    failure_streak = _trailing_failure_streak(
        per_day_kcal, failure_end, window_start, goal,
    )
    if failure_streak < MIN_FAILURE_STREAK:
        out = ReturningSuccessInsight(
            detected=False,
            recovery_days=recovery_days,
            failure_streak_days=failure_streak,
        )
        cache.set(cache_key, out, CACHE_TTL_SECONDS)
        return out

    since = today - timedelta(days=recovery_days - 1)
    out = ReturningSuccessInsight(
        detected=True,
        failure_streak_days=failure_streak,
        recovery_days=recovery_days,
        since_recovery_started=since.isoformat(),
    )
    cache.set(cache_key, out, CACHE_TTL_SECONDS)
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_goal(profile: NutritionProfile | None) -> int:
    if profile and profile.daily_kcal:
        return profile.daily_kcal
    return int(getattr(settings, "NUTRITION_DEFAULT_CALORIES_GOAL", 2000))


def _per_day_kcal(user_id: int, start: date, end: date) -> dict[date, float]:
    rows = (
        FoodLog.objects
        .filter(
            user_id=user_id,
            logged_at__gte=datetime.combine(start, time.min, tzinfo=dt_tz.utc),
            logged_at__lte=datetime.combine(end, time.max, tzinfo=dt_tz.utc),
        )
        .annotate(day=TruncDate("logged_at", tzinfo=dt_tz.utc))
        .values("day")
        .annotate(total=Sum("calories"))
    )
    return {row["day"]: float(row["total"] or 0.0) for row in rows}


def _trailing_recovery_streak(
    per_day: dict[date, float], today: date, goal: float,
) -> int:
    streak = 0
    cursor = today
    lo, hi = goal * RECOVERY_LO_PCT, goal * RECOVERY_HI_PCT
    while cursor in per_day and lo <= per_day[cursor] <= hi:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _trailing_failure_streak(
    per_day: dict[date, float], end: date, window_start: date, goal: float,
) -> int:
    streak = 0
    cursor = end
    threshold = goal * FAILURE_THRESHOLD_PCT
    while cursor >= window_start and cursor in per_day and per_day[cursor] < threshold:
        streak += 1
        cursor -= timedelta(days=1)
    return streak
