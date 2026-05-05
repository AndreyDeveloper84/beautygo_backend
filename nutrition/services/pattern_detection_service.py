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
# DRF-262: omega-3 and calcium detectors need 21-day windows. Collect
# everything up to the longest window once so per-detector queries don't
# need their own DB roundtrips.
WINDOW_MAX_DAYS = 21

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


# DRF-262: Track E micronutrient patterns. Five detectors that compare
# per-day intake to RDA targets (from NutritionProfile when DRF-265 has
# landed, settings defaults otherwise). Quality gate: any window with
# >40% AI-estimated rows is skipped (low-confidence data → bogus
# deficits → bogus cross-domain recommendations).

# Window thresholds — pinned per-rule.
LOW_VITAMIN_D_WINDOW = 7
LOW_VITAMIN_D_PCT_RDA = 0.30
LOW_VITAMIN_D_MIN_DAYS = 5

LOW_IRON_WINDOW = 14
LOW_IRON_PCT_RDA = 0.50
LOW_IRON_MIN_DAYS = 7

LOW_OMEGA3_WINDOW = 21
LOW_OMEGA3_PCT_RDA = 0.50
LOW_OMEGA3_MIN_DAYS = 14

LOW_CALCIUM_WINDOW = 21
LOW_CALCIUM_PCT_RDA = 0.50
LOW_CALCIUM_MIN_DAYS = 14

LOW_B12_WINDOW = 14
LOW_B12_PCT_RDA = 0.30
LOW_B12_MIN_DAYS = 7

# Quality gate: skip windows where AI-estimate rows exceed this share.
AI_ESTIMATE_WINDOW_LIMIT = 0.40

# Default RDAs (USDA / NIH ODS) used when NutritionProfile lacks the
# DRF-265 daily_*_norm fields. Override via settings if needed.
_DEFAULT_RDA = {
    "vitamin_d_iu": 600,
    "iron_mg": 18,
    "omega3_g": 1.1,
    "calcium_mg": 1000,
    "b12_mcg": 2.4,
}


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
    # DRF-262: micronutrient daily totals.
    total_vitamin_d_iu: float = 0.0
    total_iron_mg: float = 0.0
    total_omega3_g: float = 0.0
    total_calcium_mg: float = 0.0
    total_vitamin_b12_mcg: float = 0.0
    total_rows: int = 0
    ai_estimate_rows: int = 0
    # DRF-262 LB-6 fix: per-micronutrient row counters for the quality
    # gate. Each detector now scopes its gate to rows that actually
    # carry the micronutrient it cares about — AI-estimate snacks
    # without iron data no longer "vote" against the iron detector's
    # gate. Increment when the corresponding column is non-null.
    rows_with_vitamin_d: int = 0
    rows_with_iron: int = 0
    rows_with_omega3: int = 0
    rows_with_calcium: int = 0
    rows_with_b12: int = 0
    ai_rows_with_vitamin_d: int = 0
    ai_rows_with_iron: int = 0
    ai_rows_with_omega3: int = 0
    ai_rows_with_calcium: int = 0
    ai_rows_with_b12: int = 0


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
    # DRF-262: collect over the longest window any detector needs (21d).
    # Detectors then re-window over their own shorter slices. Single DB
    # roundtrip beats per-detector queries.
    window_start = today - timedelta(days=WINDOW_MAX_DAYS - 1)

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
        # DRF-262 — Track E micronutrient detectors.
        _detect_low_vitamin_d,
        _detect_low_iron,
        _detect_low_omega3,
        _detect_low_calcium,
        _detect_low_b12,
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
        .values(
            "dish_name", "calories", "protein_g", "logged_at",
            # DRF-262: micronutrients + provenance for the quality gate.
            "vitamin_d_iu", "iron_mg", "omega3_g",
            "calcium_mg", "vitamin_b12_mcg",
            "micronutrients_source",
        )
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
        # DRF-262 — sum micronutrients (None = 0 for aggregation).
        s.total_vitamin_d_iu += float(row.get("vitamin_d_iu") or 0.0)
        s.total_iron_mg += float(row.get("iron_mg") or 0.0)
        s.total_omega3_g += float(row.get("omega3_g") or 0.0)
        s.total_calcium_mg += float(row.get("calcium_mg") or 0.0)
        s.total_vitamin_b12_mcg += float(row.get("vitamin_b12_mcg") or 0.0)
        s.total_rows += 1
        is_ai = row.get("micronutrients_source") == "ai_estimate"
        if is_ai:
            s.ai_estimate_rows += 1
        # DRF-262 LB-6 fix: per-micronutrient counters. A row "votes"
        # against the gate of micronutrient X only if it actually
        # carries an X value. Otherwise an AI-estimate snack with
        # only macros falsely blocks every detector.
        if row.get("vitamin_d_iu") is not None:
            s.rows_with_vitamin_d += 1
            if is_ai:
                s.ai_rows_with_vitamin_d += 1
        if row.get("iron_mg") is not None:
            s.rows_with_iron += 1
            if is_ai:
                s.ai_rows_with_iron += 1
        if row.get("omega3_g") is not None:
            s.rows_with_omega3 += 1
            if is_ai:
                s.ai_rows_with_omega3 += 1
        if row.get("calcium_mg") is not None:
            s.rows_with_calcium += 1
            if is_ai:
                s.ai_rows_with_calcium += 1
        if row.get("vitamin_b12_mcg") is not None:
            s.rows_with_b12 += 1
            if is_ai:
                s.ai_rows_with_b12 += 1
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
# DRF-262 — Track E micronutrient detectors
# ---------------------------------------------------------------------------


def _rda(profile: NutritionProfile | None, key: str) -> float:
    """Return the RDA target. Reads NutritionProfile.daily_*_norm when
    available (DRF-265), falls back to settings, finally to a baked-in
    default. Tests override via settings.NUTRITION_DEFAULT_*.
    """
    profile_attr_map = {
        "vitamin_d_iu": "daily_vitamin_d_iu",
        "iron_mg": "daily_iron_mg",
        "omega3_g": "daily_omega3_g",
        "calcium_mg": "daily_calcium_mg",
        "b12_mcg": "daily_vitamin_b12_mcg",
    }
    settings_attr_map = {
        "vitamin_d_iu": "NUTRITION_DEFAULT_VITAMIN_D_IU",
        "iron_mg": "NUTRITION_DEFAULT_IRON_MG",
        "omega3_g": "NUTRITION_DEFAULT_OMEGA3_G",
        "calcium_mg": "NUTRITION_DEFAULT_CALCIUM_MG",
        "b12_mcg": "NUTRITION_DEFAULT_B12_MCG",
    }
    if profile is not None:
        attr = profile_attr_map[key]
        val = getattr(profile, attr, 0)
        if val:
            return float(val)
    settings_val = getattr(settings, settings_attr_map[key], None)
    if settings_val:
        return float(settings_val)
    return float(_DEFAULT_RDA[key])


def _quality_gate_ok(food_stats: dict, window: list, micro: str) -> bool:
    """Return True if AI-estimate share is ≤ limit for the given micro.

    DRF-262 LB-6 fix: scoped per-micronutrient. Earlier version divided
    total AI rows by total rows, so an AI-estimate snack with only
    macros (no iron data) would still vote against the iron gate.
    Now each detector cares only about rows that actually carry its
    micronutrient.

    ``micro`` ∈ {"vitamin_d", "iron", "omega3", "calcium", "b12"}.
    """
    rows_attr = f"rows_with_{micro}"
    ai_attr = f"ai_rows_with_{micro}"
    rows = sum(
        getattr(food_stats[d], rows_attr) for d in window
        if d in food_stats
    )
    if rows == 0:
        # No data with this micronutrient — caller's threshold check
        # short-circuits anyway (low_days = []), so gate-pass here
        # is harmless and avoids spurious blocks.
        return True
    ai_rows = sum(
        getattr(food_stats[d], ai_attr) for d in window
        if d in food_stats
    )
    return (ai_rows / rows) <= AI_ESTIMATE_WINDOW_LIMIT


def _detect_low_vitamin_d(*, food_stats, water_stats, profile, today):
    rda = _rda(profile, "vitamin_d_iu")
    if rda <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(LOW_VITAMIN_D_WINDOW)]
    if not _quality_gate_ok(food_stats, window, "vitamin_d"):
        return None
    threshold = rda * LOW_VITAMIN_D_PCT_RDA
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and 0 < s.total_vitamin_d_iu < threshold
    ]
    if len(low_days) < LOW_VITAMIN_D_MIN_DAYS:
        return None
    return _build_pattern(
        slug="low_vitamin_d",
        name_ru="Дефицит витамина D",
        count=len(low_days),
        window_days=LOW_VITAMIN_D_WINDOW,
        threshold=LOW_VITAMIN_D_MIN_DAYS,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_iu": int(rda),
            "frequency_text": f"{len(low_days)} дней из {LOW_VITAMIN_D_WINDOW}",
        },
    )


def _detect_low_iron(*, food_stats, water_stats, profile, today):
    rda = _rda(profile, "iron_mg")
    if rda <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(LOW_IRON_WINDOW)]
    if not _quality_gate_ok(food_stats, window, "iron"):
        return None
    threshold = rda * LOW_IRON_PCT_RDA
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and 0 < s.total_iron_mg < threshold
    ]
    if len(low_days) < LOW_IRON_MIN_DAYS:
        return None
    return _build_pattern(
        slug="low_iron",
        name_ru="Низкое железо",
        count=len(low_days),
        window_days=LOW_IRON_WINDOW,
        threshold=LOW_IRON_MIN_DAYS,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_mg": float(rda),
            "frequency_text": f"{len(low_days)} дней из {LOW_IRON_WINDOW}",
        },
    )


def _detect_low_omega3(*, food_stats, water_stats, profile, today):
    rda = _rda(profile, "omega3_g")
    if rda <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(LOW_OMEGA3_WINDOW)]
    if not _quality_gate_ok(food_stats, window, "omega3"):
        return None
    threshold = rda * LOW_OMEGA3_PCT_RDA
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and 0 < s.total_omega3_g < threshold
    ]
    if len(low_days) < LOW_OMEGA3_MIN_DAYS:
        return None
    pattern = _build_pattern(
        slug="low_omega3",
        name_ru="Дефицит омега-3",
        count=len(low_days),
        window_days=LOW_OMEGA3_WINDOW,
        threshold=LOW_OMEGA3_MIN_DAYS,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_g": float(rda),
            "frequency_text": f"{len(low_days)} дней из {LOW_OMEGA3_WINDOW}",
        },
    )
    # Pin severity at "low" — chronic but not urgent.
    return _force_severity(pattern, "low")


def _detect_low_calcium(*, food_stats, water_stats, profile, today):
    rda = _rda(profile, "calcium_mg")
    if rda <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(LOW_CALCIUM_WINDOW)]
    if not _quality_gate_ok(food_stats, window, "calcium"):
        return None
    threshold = rda * LOW_CALCIUM_PCT_RDA
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and 0 < s.total_calcium_mg < threshold
    ]
    if len(low_days) < LOW_CALCIUM_MIN_DAYS:
        return None
    pattern = _build_pattern(
        slug="low_calcium",
        name_ru="Низкий кальций",
        count=len(low_days),
        window_days=LOW_CALCIUM_WINDOW,
        threshold=LOW_CALCIUM_MIN_DAYS,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_mg": int(rda),
            "frequency_text": f"{len(low_days)} дней из {LOW_CALCIUM_WINDOW}",
        },
    )
    return _force_severity(pattern, "low")


def _detect_low_b12(*, food_stats, water_stats, profile, today):
    rda = _rda(profile, "b12_mcg")
    if rda <= 0:
        return None
    window = [today - timedelta(days=i) for i in range(LOW_B12_WINDOW)]
    if not _quality_gate_ok(food_stats, window, "b12"):
        return None
    threshold = rda * LOW_B12_PCT_RDA
    low_days = [
        d for d in window
        if (s := food_stats.get(d))
        and 0 < s.total_vitamin_b12_mcg < threshold
    ]
    if len(low_days) < LOW_B12_MIN_DAYS:
        return None
    pattern = _build_pattern(
        slug="low_b12",
        name_ru="Дефицит B12",
        count=len(low_days),
        window_days=LOW_B12_WINDOW,
        threshold=LOW_B12_MIN_DAYS,
        recent_dates=[d.isoformat() for d in sorted(low_days, reverse=True)[:5]],
        args={
            "target_mcg": float(rda),
            "frequency_text": f"{len(low_days)} дней из {LOW_B12_WINDOW}",
        },
    )
    # Severity bumped to high for vegans/vegetarians — B12 is plant-poor
    # so deficit is more concerning when supplementation isn't likely.
    flags = (profile.health_flags or {}) if profile else {}
    if flags.get("vegan") or flags.get("vegetarian"):
        return _force_severity(pattern, "high")
    return _force_severity(pattern, "medium")


def _force_severity(pattern: DetectedPattern, severity: str) -> DetectedPattern:
    """Override ``severity`` (and the derived ``display_hint``) on a
    pattern that ``_build_pattern`` produced. Frozen dataclass requires
    a fresh instance.
    """
    return DetectedPattern(
        slug=pattern.slug,
        name_ru=pattern.name_ru,
        count=pattern.count,
        active_window_days=pattern.active_window_days,
        severity=severity,
        recent_dates=pattern.recent_dates,
        advice_template_args=pattern.advice_template_args,
        display_hint=_display_hint(severity),
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
    # DRF-262 LB-5 fix: severity="low" must NOT map to "hidden". Track E
    # micronutrient detectors (low_omega3, low_calcium) intentionally
    # ship at low severity — chronic but not urgent — and the
    # cross-domain rule engine still needs them visible. Map low →
    # "secondary" so they queue behind primary patterns rather than
    # being silently dropped.
    return {
        "high": "primary",
        "medium": "secondary",
        "low": "secondary",
    }.get(severity, "hidden")


def _suppressed_by_flags(slug: str, flags: dict) -> bool:
    # DRF-262: all five micronutrient patterns are suppressed for ED.
    # We never frame a deficit story to ED users — pattern engine
    # silently swallows the result.
    micronutrient_slugs = {
        "low_vitamin_d", "low_iron", "low_omega3",
        "low_calcium", "low_b12",
    }
    if flags.get("eating_disorder"):
        if slug in {"frequent_alcohol", "low_protein", "meal_skips"}:
            return True
        if slug in micronutrient_slugs:
            return True
    if flags.get("pregnant") and slug == "frequent_alcohol":
        # Medical concern — flag is upstream (caregiver), not a nudge.
        return True
    return False
