"""NutritionProfile compute + override ladder (DRF-300).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §1.

Pure-math: BMR (Mifflin-St Jeor), daily targets (kcal/protein/fat/carbs/
water), and the override ladder that coerces unsafe goals into safe
ones. No LLM, no external API, deterministic — every input maps to one
output and the override audit is fully reproducible.

Override priority (highest first):
  1. ``eating_disorder=True`` → goal=maintain unconditionally (we never
     show numeric deficits to ED users; the bot strips them downstream)
  2. ``pregnant`` or ``breastfeeding`` → goal=maintain + extra kcal
     and protein (200/400 + 25g)
  3. BMR floor ladder — if computed daily_kcal < BMR + 100:
     - first try pace=gentle (smaller deficit)
     - if still under floor, fall back to goal=maintain

Defaults (per spec §1.2): if a field is in ``_skipped_fields``, fill
with the median of the Penza pilot audience — ``female / 40 / 165 / 70``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


# Defaults for skipped anthropometry fields — Penza pilot audience median.
DEFAULT_GENDER = "female"
DEFAULT_AGE = 40
DEFAULT_HEIGHT_CM = 165
DEFAULT_WEIGHT_KG = 70.0
DEFAULT_ACTIVITY = 1.4

# BMR floor margin — daily_kcal must stay at least this far above BMR
# before we accept a deficit. Anything tighter would mean eating less
# than the body's resting expenditure, which is the canonical
# under-eating boundary in nutrition guidelines.
BMR_FLOOR_MARGIN_KCAL = 100

# Goal multipliers applied to TDEE = BMR × activity_coefficient.
GOAL_FACTORS = {
    "lose": 0.80,        # ~20% deficit
    "tone": 0.90,        # mild deficit + protein priority
    "maintain": 1.00,
    "gain": 1.10,        # ~10% surplus
}

PACE_FACTORS = {
    "gentle": 0.92,      # softer effective deficit/surplus
    "moderate": 1.00,
}

# Pregnancy / breastfeeding kcal & protein deltas (spec §1.2).
PREGNANCY_KCAL_BONUS = 200
BREASTFEEDING_KCAL_BONUS = 400
PREGNANCY_PROTEIN_BONUS_G = 25

# Per-kg water target. Spec: 30 ml/kg + adjustments. Lightweight model
# for now — adjustments come in Phase 3.2 (heat/exercise/breastfeeding).
WATER_ML_PER_KG = 30


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class ProfileInputs:
    """All upsert-time inputs needed by ``compute_norms``.

    Mirrors the fields the bot's POST body sets — we never read
    NutritionProfile directly here so the function stays pure and easy
    to fuzz-test.
    """
    gender: str = DEFAULT_GENDER
    age: int | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    activity_coefficient: float = DEFAULT_ACTIVITY
    goal: str = "maintain"
    pace: str = "moderate"
    health_flags: dict = field(default_factory=dict)


@dataclass
class ComputedNorms:
    bmr: int
    daily_kcal: int
    daily_protein_g: int
    daily_fat_g: int
    daily_carbs_g: int
    daily_water_ml: int
    goal: str
    pace: str
    goal_overridden_by: str
    overrides_applied: list[dict[str, Any]] = field(default_factory=list)

    # DRF-265: micronutrient RDA targets (USDA / NIH ODS-derived).
    # Filled by compute_rda(); pure ints/floats — no override audit
    # because RDA adjustments are deterministic (age/gender/flags-driven).
    daily_vitamin_d_iu: int = 0
    daily_vitamin_b12_mcg: float = 0.0
    daily_vitamin_c_mg: int = 0
    daily_iron_mg: float = 0.0
    daily_calcium_mg: int = 0
    daily_magnesium_mg: int = 0
    daily_omega3_g: float = 0.0
    daily_fiber_g: int = 0


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_norms(inputs: ProfileInputs) -> ComputedNorms:
    """Pure deterministic computation.

    Fills defaults for skipped fields, computes BMR, applies the
    override ladder in priority order, then derives daily targets.
    """
    gender = inputs.gender or DEFAULT_GENDER
    age = inputs.age if inputs.age is not None else DEFAULT_AGE
    height_cm = inputs.height_cm if inputs.height_cm is not None else DEFAULT_HEIGHT_CM
    weight_kg = inputs.weight_kg if inputs.weight_kg is not None else DEFAULT_WEIGHT_KG

    bmr = _mifflin_st_jeor(gender, age, height_cm, weight_kg)
    activity = inputs.activity_coefficient or DEFAULT_ACTIVITY
    goal = inputs.goal or "maintain"
    pace = inputs.pace or "moderate"
    flags = inputs.health_flags or {}

    overrides: list[dict] = []
    overridden_by = ""
    bonus_kcal = 0
    bonus_protein_g = 0

    # 1) eating disorder — strongest override
    if flags.get("eating_disorder"):
        if goal != "maintain":
            overrides.append({
                "reason": "eating_disorder",
                "from": {"goal": goal},
                "to": {"goal": "maintain"},
            })
            goal = "maintain"
        overridden_by = "eating_disorder"

    # 2) pregnancy / breastfeeding — only fires if ED didn't already pin.
    elif flags.get("pregnant") or flags.get("breastfeeding"):
        reason = "pregnancy" if flags.get("pregnant") else "breastfeeding"
        if goal == "lose":
            overrides.append({
                "reason": reason,
                "from": {"goal": "lose"},
                "to": {"goal": "maintain"},
            })
            goal = "maintain"
        bonus_kcal = (
            BREASTFEEDING_KCAL_BONUS
            if flags.get("breastfeeding")
            else PREGNANCY_KCAL_BONUS
        )
        bonus_protein_g = PREGNANCY_PROTEIN_BONUS_G
        overridden_by = reason

    # 3) BMR floor ladder — only when goal=lose and we'd undercut BMR
    daily_kcal = _kcal_from_goal(bmr, activity, goal, pace) + bonus_kcal

    if goal == "lose" and daily_kcal < bmr + BMR_FLOOR_MARGIN_KCAL:
        if pace == "moderate":
            overrides.append({
                "reason": "bmr_floor",
                "from": {"pace": "moderate"},
                "to": {"pace": "gentle"},
            })
            pace = "gentle"
            daily_kcal = _kcal_from_goal(bmr, activity, goal, pace) + bonus_kcal

        if daily_kcal < bmr + BMR_FLOOR_MARGIN_KCAL:
            overrides.append({
                "reason": "bmr_floor",
                "from": {"goal": "lose"},
                "to": {"goal": "maintain"},
            })
            goal = "maintain"
            overridden_by = overridden_by or "bmr_floor"
            daily_kcal = _kcal_from_goal(bmr, activity, goal, pace) + bonus_kcal

    protein_g, fat_g, carbs_g = _macros_split(daily_kcal, weight_kg, goal)
    protein_g += bonus_protein_g
    daily_water_ml = _water_target(weight_kg, flags)

    rda = compute_rda(
        gender=gender,
        age=age,
        health_flags=flags,
    )

    return ComputedNorms(
        bmr=int(round(bmr)),
        daily_kcal=int(round(daily_kcal)),
        daily_protein_g=int(round(protein_g)),
        daily_fat_g=int(round(fat_g)),
        daily_carbs_g=int(round(carbs_g)),
        daily_water_ml=int(round(daily_water_ml)),
        goal=goal,
        pace=pace,
        goal_overridden_by=overridden_by,
        overrides_applied=overrides,
        # DRF-265: RDA layer — independent of macro override ladder.
        daily_vitamin_d_iu=rda["vitamin_d_iu"],
        daily_vitamin_b12_mcg=rda["vitamin_b12_mcg"],
        daily_vitamin_c_mg=rda["vitamin_c_mg"],
        daily_iron_mg=rda["iron_mg"],
        daily_calcium_mg=rda["calcium_mg"],
        daily_magnesium_mg=rda["magnesium_mg"],
        daily_omega3_g=rda["omega3_g"],
        daily_fiber_g=rda["fiber_g"],
    )


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _mifflin_st_jeor(gender: str, age: int, height_cm: int, weight_kg: float) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if gender == "male" else -161)


def _kcal_from_goal(bmr: float, activity: float, goal: str, pace: str) -> float:
    tdee = bmr * activity
    goal_factor = GOAL_FACTORS.get(goal, 1.0)
    pace_factor = PACE_FACTORS.get(pace, 1.0)
    # Pace softens deficits/surpluses but doesn't move maintain. The
    # multiplicative model: kcal = TDEE × (1 + pace_factor × (goal-1)).
    delta = (goal_factor - 1.0) * pace_factor
    return tdee * (1.0 + delta)


def _macros_split(daily_kcal: float, weight_kg: float, goal: str) -> tuple[float, float, float]:
    """Spec §1.2 implies a goal-aware split. Use a defensible default:
    protein floor 1.6 g/kg (lose/tone), 1.4 g/kg otherwise; fat 30% of
    kcal; carbs fill the remainder.
    """
    protein_per_kg = 1.6 if goal in ("lose", "tone") else 1.4
    protein_g = protein_per_kg * weight_kg
    fat_g = (daily_kcal * 0.30) / 9.0
    used_kcal = protein_g * 4 + fat_g * 9
    carbs_g = max(0.0, (daily_kcal - used_kcal) / 4.0)
    return protein_g, fat_g, carbs_g


def _water_target(weight_kg: float, flags: dict) -> float:
    base = WATER_ML_PER_KG * weight_kg
    # Pregnancy / breastfeeding nudge — small, conservative additions.
    if flags.get("pregnant"):
        base += 300
    if flags.get("breastfeeding"):
        base += 700
    return base


# ---------------------------------------------------------------------------
# DRF-265: RDA (recommended daily allowance) for micronutrients
# ---------------------------------------------------------------------------


# Baseline RDA tables. Values from USDA / NIH ODS reference intakes.
# Keyed by ``(gender, age_band)``. Age bands kept coarse on purpose —
# pattern engine doesn't need single-year granularity, and mobile UX
# already only collects an integer year.

# Adolescent: 14-18, Adult: 19-50 (women's iron drops at 50 → menopause),
# Mature: 51-64 (post-menstrual women), Senior: 65+.
_AGE_BANDS = ("adolescent", "adult", "mature", "senior")


def _age_band(age: int) -> str:
    if age < 19:
        return "adolescent"
    if age < 51:
        return "adult"
    if age < 65:
        return "mature"
    return "senior"


_BASE_RDA: dict[tuple[str, str], dict] = {
    # Adult women 19-50.
    ("female", "adult"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 18,
        "calcium_mg": 1000, "magnesium_mg": 310,
        "omega3_g": 1.1, "fiber_g": 25,
    },
    # Adult men 19-50 — iron drops, magnesium up.
    ("male", "adult"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1000, "magnesium_mg": 400,
        "omega3_g": 1.6, "fiber_g": 38,
    },
    # Adolescent girls 14-18 — calcium up for bone development, iron 15.
    ("female", "adolescent"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 65, "iron_mg": 15,
        "calcium_mg": 1300, "magnesium_mg": 360,
        "omega3_g": 1.1, "fiber_g": 26,
    },
    # Adolescent boys 14-18.
    ("male", "adolescent"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 11,
        "calcium_mg": 1300, "magnesium_mg": 410,
        "omega3_g": 1.6, "fiber_g": 38,
    },
    # Mature women 51-64 — iron drops to 8 (menopause), bone preservation.
    ("female", "mature"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 320,
        "omega3_g": 1.1, "fiber_g": 21,
    },
    ("male", "mature"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1000, "magnesium_mg": 420,
        "omega3_g": 1.6, "fiber_g": 30,
    },
    # Senior 65+ — vitamin D up to 800, women's calcium 1200.
    ("female", "senior"): {
        "vitamin_d_iu": 800, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 320,
        "omega3_g": 1.1, "fiber_g": 21,
    },
    ("male", "senior"): {
        "vitamin_d_iu": 800, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 420,
        "omega3_g": 1.6, "fiber_g": 30,
    },
}


def compute_rda(
    *, gender: str, age: int, health_flags: dict,
) -> dict[str, Any]:
    """Compute micronutrient RDA targets for the user.

    Pure-math mirror of ``compute_norms`` (kcal/protein/...). Reads
    age + gender as the base index, then layers pregnancy, breastfeeding,
    and dietary flags on top. No LLM, no external calls.
    """
    band = _age_band(age)
    base = dict(_BASE_RDA.get((gender, band), _BASE_RDA[("female", "adult")]))

    # Pregnancy — bumps iron, omega-3, fibre. Calcium stays at 1000
    # (USDA RDA for pregnancy in women 19-50). Pregnancy ≫ flag wins
    # over breastfeeding when both are set (rare but possible).
    if health_flags.get("pregnant"):
        base["iron_mg"] = 27
        base["calcium_mg"] = 1000
        base["omega3_g"] = 1.4
        base["fiber_g"] = 28
        base["vitamin_c_mg"] = 85

    elif health_flags.get("breastfeeding"):
        # Breastfeeding bumps calcium / vit C / omega-3, drops iron
        # (no menstrual loss → 9 mg).
        base["calcium_mg"] = 1000
        base["vitamin_c_mg"] = 120
        base["omega3_g"] = 1.3
        base["iron_mg"] = 9

    # Dietary restriction — vegan multiplier on B12 & iron.
    # Non-heme iron from plants is ~1.8× less bioavailable; B12 doesn't
    # come from plant foods at all, so requirement is ~doubled to push
    # supplementation guidance.
    if health_flags.get("vegan"):
        base["vitamin_b12_mcg"] = 4.0
        base["iron_mg"] = round(base["iron_mg"] * 1.8)
    elif health_flags.get("vegetarian"):
        base["vitamin_b12_mcg"] = 3.0
        # Vegetarians who eat eggs/dairy still get heme-adjacent iron;
        # we apply a milder ×1.3 bump.
        base["iron_mg"] = round(base["iron_mg"] * 1.3, 1)

    return base
