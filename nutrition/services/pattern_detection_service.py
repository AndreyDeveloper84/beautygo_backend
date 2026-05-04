"""Behavioural pattern detection (DRF-304).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §3.1.

Seven server-side detectors run over the last 14 days of FoodLog +
WaterEntry data and emit ``pattern_detected`` signals the bot's nudge
engine can render into «вечерние срывы», «низкий белок», etc.

Each detector:
- Is a pure function over the per-day stat tables prepared once
  upfront (one DB roundtrip per source table).
- Returns ``DetectedPattern`` or ``None``.
- Carries ``advice_template_args`` for the bot's templating.

Health-flag suppression rules (spec §3.1):
- ``eating_disorder=true``  → suppress ``frequent_alcohol`` (we never
  pile on the user) and ``low_protein`` (numbers stripped under ED).
- ``pregnant=true``          → keep ``late_caffeine`` even at lower
  threshold; but suppress ``frequent_alcohol`` (medical concern is
  upstream).

display_hint priorities (UX): high severity = ``primary`` (one shown at
a time), medium = ``secondary`` (queued), below threshold = ``hidden``
(not returned).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from typing import Any

from django.conf import settings
from django.core.cache import cache

from nutrition.models import Beverage, FoodLog, NutritionProfile, WaterEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

WINDOW_LONG_DAYS = 14
WINDOW_SHORT_DAYS = 7
WINDOW_STREAK_DAYS = 3

CACHE_TTL_SECONDS = 12 * 60 * 60

LOW_PROTEIN_PCT = 0.70
LOW_WATER_PCT = 0.70
MEAL_SKIP_PCT = 0.30
LATE_DINNER_HOUR = 21
EVENING_HOUR = 19
LATE_CAFFEINE_HOUR = 17

EVENING_SWEETS_MIN = 4
LOW_PROTEIN_MIN = 5
LOW_WATER_MIN = 4
LATE_DINNER_MIN = 3
LATE_CAFFEINE_MIN = 3
FREQUENT_ALCOHOL_MIN = 2
FREQUENT_ALCOHOL_REQUIRES_HISTORY = 14

SWEET_KEYWORDS = (
    "шоколад", "конфет", "торт", "пирож", "мороженое",
    "печенье", "пирог", "круассан", "кекс", "зефир", "пастил",
    "пряник", "вафл", "сырок", "халва", "карамел",
    "десерт", "сладкое", "булочк",
)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedPattern:
    slug: str
    name_ru: str
    count: int
    active_window_days: int
    severity: str           # "low" | "medium" | "high"
    recent_dates: list[str]
    advice_template_args: dict[str, Any]
    display_hint: str       # "primary" | "secondary" | "hidden"


@dataclass
class DailyFoodStats:
    """One row per UTC day with cached aggregates from FoodLog."""
    day: date
    total_kcal: float = 0.0
    total_protein_g: float = 0.0
    last_meal_dt: datetime | None = None
    has_evening_sweets: bool = False
    weekday: int = 0  # 0..6


@dataclass
class DailyWaterStats:
    day: date
    total_water_ml: float = 0.0
    total_caffeine_mg: float = 0.0
    has_late_caffeine: bool = False
    has_alcohol: bool = False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass
class PatternDetectionResult:
    active_days: int
    patterns: list[DetectedPattern] = field(default_factory=list)


def detect_patterns(*, user_id: int, force: bool = False) -> PatternDetectionResult:
    """Top-level orchestration. Cached for 12h per user (spec §13).

    ``force=True`` bypasses cache — used by admin actions and tests.
    """
    cache_key = f"nutrition.patterns:{user_id}"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    today = datetime.now(dt_tz.utc).date()
    window_start = today - timedelta(days=WINDOW_LONG_DAYS - 1)

    profile = NutritionProfile.objects.filter(user_id=user_id).first()
    flags = (profile.health_flags or {}) if profile else {}

    food_stats = _collect_food_stats(user_id, window_start, today)
    water_stats = _collect_water_stats(user_id, window_start, today)
    active_days = len({s.day for s in food_stats.values()} |
                      {s.day for s in water_stats.values()})

    detectors = (
        _detect_evening_sweets,
        _detect_low_protein,
        _detect_low_water,
        _detect_late_dinner,
        _detect_meal_skips,
        _detect_late_caffeine,
        _detect_frequent_alcohol,
    )

    patterns: list[DetectedPattern] = []
    for detector in detectors:
        try:
            result = detector(
                food_stats=food_stats,
                water_stats=water_stats,
                profile=profile,
                today=today,
            )
        except Exception as exc:  # noqa: BLE001 — never crash whole pipeline on one bad rule
            logger.warning(
                "nutrition.pattern.detector_error rule=%s err=%s",
                detector.__name__, exc,
            )
            continue
        if result is None:
            continue
        if _suppressed_by_flags(result.slug, flags):
            continue
        patterns.append(result)

    out = PatternDetectionResult(active_days=active_days, patterns=patterns)
    cache.set(cache_key, out, CACHE_TTL_SECONDS)
    return out


# ---------------------------------------------------------------------------
# Aggregators (one DB roundtrip each)
# ---------------------------------------------------------------------------


def _collect_food_stats(
    user_id: int, start: date, end: date,
) -> dict[date, DailyFoodStats]:
    rows = (
        FoodLog.objects
        .filter(
            user_id=user_id,
            logged_at__gte=datetime.combine(start, time.min, tzinfo=dt_tz.utc),
            logged_at__lte=datetime.combine(end, time.max, tzinfo=dt_tz.utc),
        )
        .values("dish_name", "calories", "protein_g", "logged_at")
    )

    stats: dict[date, DailyFoodStats] = {}
    for row in rows:
        ts: datetime = row["logged_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_tz.utc)
        d = ts.astimezone(dt_tz.utc).date()
        s = stats.setdefault(d, DailyFoodStats(day=d, weekday=d.weekday()))
        s.total_kcal += float(row["calories"] or 0.0)
        s.total_protein_g += float(row["protein_g"] or 0.0)
        if s.last_meal_dt is None or ts > s.last_meal_dt:
            s.last_meal_dt = ts
        if not s.has_evening_sweets and ts.hour >= EVENING_HOUR:
            name = (row["dish_name"] or "").lower()
            if any(kw in name for kw in SWEET_KEYWORDS):
                s.has_evening_sweets = True
    return stats


def _collect_water_stats(
    user_id: int, start: date, end: date,
) -> dict[date, DailyWaterStats]:
    rows = (
        WaterEntry.objects
        .filter(
            user_id=user_id,
            ts__gte=datetime.combine(start, time.min, tzinfo=dt_tz.utc),
            ts__lte=datetime.combine(end, time.max, tzinfo=dt_tz.utc),
            deleted_at__isnull=True,
        )
        .select_related("beverage")
        .values(
            "ts", "water_ml", "caffeine_mg",
            "beverage__category",
        )
    )
    stats: dict[date, DailyWaterStats] = {}
    for row in rows:
        ts: datetime = row["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt_tz.utc)
        d = ts.astimezone(dt_tz.utc).date()
        s = stats.setdefault(d, DailyWaterStats(day=d))
        s.total_water_ml += float(row["water_ml"] or 0.0)
        s.total_caffeine_mg += float(row["caffeine_mg"] or 0.0)
        if (row["caffeine_mg"] or 0) > 0 and ts.hour >= LATE_CAFFEINE_HOUR:
            s.has_late_caffeine = True
        if row["beverage__category"] == Beverage.Category.ALCOHOL:
            s.has_alcohol = True
    return stats


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------


def _detect_evening_sweets(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    window = [today - timedelta(days=i) for i in range(WINDOW_LONG_DAYS)]
    weekday_sweets = [
        d for d in window
        if (s := food_stats.get(d))
        and s.has_evening_sweets and s.weekday < 5
    ]
    count = len(weekday_sweets)
    if count < EVENING_SWEETS_MIN:
        return None
    return _build_pattern(
        slug="evening_sweets",
        name_ru="Вечерние срывы на сладкое",
        count=count,
        window_days=WINDOW_LONG_DAYS,
        threshold=EVENING_SWEETS_MIN,
        recent_dates=[d.isoformat() for d in sorted(weekday_sweets, reverse=True)[:5]],
        args={
            "trigger_time": f"после {EVENING_HOUR}:00",
            "weekday_pattern": "будни",
            "frequency_text": f"{count} раз за {WINDOW_LONG_DAYS} дней",
        },
    )


def _detect_low_protein(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    goal = _profile_goal_protein(profile)
    if goal <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(WINDOW_SHORT_DAYS)]
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and s.total_protein_g < goal * LOW_PROTEIN_PCT
    ]
    count = len(low_days)
    if count < LOW_PROTEIN_MIN:
        return None
    avg_actual = (
        sum(food_stats[d].total_protein_g for d in low_days) / count
    )
    return _build_pattern(
        slug="low_protein",
        name_ru="Низкий белок",
        count=count,
        window_days=WINDOW_SHORT_DAYS,
        threshold=LOW_PROTEIN_MIN,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "average_actual": round(avg_actual, 1),
            "target": int(goal),
            "deficit_pct": int((1 - avg_actual / goal) * 100),
        },
    )


def _detect_low_water(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    goal = _profile_goal_water(profile)
    if goal <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(WINDOW_SHORT_DAYS)]
    low_days = [
        d for d in window
        if (s := water_stats.get(d))
        and s.total_water_ml < goal * LOW_WATER_PCT
    ]
    count = len(low_days)
    if count < LOW_WATER_MIN:
        return None
    return _build_pattern(
        slug="low_water",
        name_ru="Дефицит воды",
        count=count,
        window_days=WINDOW_SHORT_DAYS,
        threshold=LOW_WATER_MIN,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_ml": int(goal),
            "frequency_text": f"{count} дней из {WINDOW_SHORT_DAYS}",
        },
    )


def _detect_late_dinner(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    window = [today - timedelta(days=i) for i in range(WINDOW_SHORT_DAYS)]
    late_days = [
        d for d in window
        if (s := food_stats.get(d))
        and s.last_meal_dt is not None
        and s.last_meal_dt.hour >= LATE_DINNER_HOUR
    ]
    count = len(late_days)
    if count < LATE_DINNER_MIN:
        return None
    return _build_pattern(
        slug="late_dinner",
        name_ru="Поздний ужин",
        count=count,
        window_days=WINDOW_SHORT_DAYS,
        threshold=LATE_DINNER_MIN,
        recent_dates=[d.isoformat() for d in sorted(late_days, reverse=True)[:5]],
        args={
            "trigger_time": f"после {LATE_DINNER_HOUR}:00",
            "frequency_text": f"{count} раз за {WINDOW_SHORT_DAYS} дней",
        },
    )


def _detect_meal_skips(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    """Three days in a row (ending today) with <30% of daily kcal goal."""
    goal = _profile_goal_kcal(profile)
    if goal <= 0:
        return None
    streak: list[date] = []
    for i in range(WINDOW_STREAK_DAYS):
        d = today - timedelta(days=i)
        s = food_stats.get(d)
        if s is None or s.total_kcal >= goal * MEAL_SKIP_PCT:
            return None
        streak.append(d)
    return _build_pattern(
        slug="meal_skips",
        name_ru="Пропуски еды",
        count=len(streak),
        window_days=WINDOW_STREAK_DAYS,
        threshold=WINDOW_STREAK_DAYS,
        recent_dates=[d.isoformat() for d in streak],
        args={
            "streak_days": len(streak),
            "target_kcal": int(goal),
            "threshold_pct": int(MEAL_SKIP_PCT * 100),
        },
    )


def _detect_late_caffeine(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    window = [today - timedelta(days=i) for i in range(WINDOW_SHORT_DAYS)]
    late_days = [
        d for d in window
        if (s := water_stats.get(d)) and s.has_late_caffeine
    ]
    count = len(late_days)
    if count < LATE_CAFFEINE_MIN:
        return None
    return _build_pattern(
        slug="late_caffeine",
        name_ru="Поздний кофеин",
        count=count,
        window_days=WINDOW_SHORT_DAYS,
        threshold=LATE_CAFFEINE_MIN,
        recent_dates=[d.isoformat() for d in sorted(late_days, reverse=True)[:5]],
        args={
            "trigger_time": f"после {LATE_CAFFEINE_HOUR}:00",
            "frequency_text": f"{count} раз за {WINDOW_SHORT_DAYS} дней",
        },
    )


def _detect_frequent_alcohol(*, food_stats, water_stats, profile, today) -> DetectedPattern | None:
    if not water_stats:
        return None
    earliest = min(s.day for s in water_stats.values())
    history_days = (today - earliest).days + 1
    if history_days < FREQUENT_ALCOHOL_REQUIRES_HISTORY:
        return None
    window = [today - timedelta(days=i) for i in range(WINDOW_SHORT_DAYS)]
    alc_days = [
        d for d in window
        if (s := water_stats.get(d)) and s.has_alcohol
    ]
    count = len(alc_days)
    if count < FREQUENT_ALCOHOL_MIN:
        return None
    return _build_pattern(
        slug="frequent_alcohol",
        name_ru="Частый алкоголь",
        count=count,
        window_days=WINDOW_SHORT_DAYS,
        threshold=FREQUENT_ALCOHOL_MIN,
        recent_dates=[d.isoformat() for d in sorted(alc_days, reverse=True)[:5]],
        args={
            "frequency_text": f"{count} дней из {WINDOW_SHORT_DAYS}",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile_goal_protein(profile: NutritionProfile | None) -> float:
    if profile and profile.daily_protein_g:
        return float(profile.daily_protein_g)
    return float(getattr(settings, "NUTRITION_DEFAULT_PROTEIN_GOAL_G", 0) or 0)


def _profile_goal_water(profile: NutritionProfile | None) -> float:
    if profile and profile.daily_water_ml:
        return float(profile.daily_water_ml)
    return float(getattr(settings, "NUTRITION_DEFAULT_WATER_GOAL_ML", 2000))


def _profile_goal_kcal(profile: NutritionProfile | None) -> float:
    if profile and profile.daily_kcal:
        return float(profile.daily_kcal)
    return float(getattr(settings, "NUTRITION_DEFAULT_CALORIES_GOAL", 2000))


def _build_pattern(
    *,
    slug: str,
    name_ru: str,
    count: int,
    window_days: int,
    threshold: int,
    recent_dates: list[str],
    args: dict[str, Any],
) -> DetectedPattern:
    severity = _severity(count, window_days, threshold)
    return DetectedPattern(
        slug=slug,
        name_ru=name_ru,
        count=count,
        active_window_days=window_days,
        severity=severity,
        recent_dates=recent_dates,
        advice_template_args=args,
        display_hint=_display_hint(severity),
    )


def _severity(count: int, window: int, threshold: int) -> str:
    if window <= 0:
        return "low"
    pct = count / window
    if pct >= 0.75:
        return "high"
    if count >= threshold:
        return "medium"
    return "low"


def _display_hint(severity: str) -> str:
    return {
        "high": "primary",
        "medium": "secondary",
    }.get(severity, "hidden")


def _suppressed_by_flags(slug: str, flags: dict) -> bool:
    if flags.get("eating_disorder"):
        # Numeric advice and pile-on suppressed for ED users (spec §10).
        if slug in {"frequent_alcohol", "low_protein", "meal_skips"}:
            return True
    if flags.get("pregnant") and slug == "frequent_alcohol":
        # Medical concern — flag is upstream (caregiver), not a nudge.
        return True
    return False
