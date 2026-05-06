"""FoodLog creation service.

Encapsulates the macro derivation logic so views stay thin and tests
can swap NutritionLookup without spinning up an HTTP client.

Two creation paths, both per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION
``POST /nutrition/food-log``:

1. **scan_id path** — caller logs an earlier scan. Macros come from
   ``FoodScan.nutrition`` (already a ``NutritionFacts.to_dict()`` JSON
   from Slice 3a) and are scaled by ``portion_multiplier``. We do NOT
   re-run the lookup — the scan's snapshot wins, even if the seed
   has since been corrected. Stable diary > consistent macros.

2. **manual path** — caller supplies ``dish_name`` (and no scan_id).
   We run ``NutritionLookup`` against a 100g baseline, then scale by
   ``portion_multiplier``. Convention: ``portion_multiplier=1.0`` means
   100g of the dish — documented in the FoodLogService docstring and
   tested.

Errors:
- Both ``scan_id`` and ``dish_name`` empty → ``ValidationError`` (caught
  by the serializer; service is defensive).
- ``scan_id`` references someone else's scan → ``ScanNotOwnedError`` →
  view returns 404 (not 403, to avoid existence leak).
- Manual ``dish_name`` doesn't resolve in seed/lookup →
  ``DishNotRecognizedError`` → view returns 400 ``FOOD_NOT_RECOGNIZED``.
- Scan exists but its nutrition is null (Slice 3a miss) →
  ``DishNotRecognizedError``.

If both ``scan_id`` and ``dish_name`` are passed, scan_id wins (more
authoritative — provider already saw the photo).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from django.utils import timezone

from nutrition.models import FoodLog, FoodScan
from nutrition.services.nutrition_lookup import NutritionLookup


# Documented baseline for manual entries — see module docstring.
MANUAL_DISH_BASELINE_G = 100.0


@dataclass(frozen=True)
class CreateFoodLogInput:
    user_id: int
    portion_multiplier: float
    meal_type: str
    scan_id: Optional[UUID] = None
    dish_name: Optional[str] = None
    logged_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None


class FoodLogServiceError(Exception):
    """Base class for service-side errors the view layer maps to HTTP."""


class ScanNotOwnedError(FoodLogServiceError):
    """Caller passed a scan_id they don't own (or that doesn't exist)."""


class DishNotRecognizedError(FoodLogServiceError):
    """Manual dish_name didn't resolve OR scan has no nutrition snapshot."""


class InvalidInputError(FoodLogServiceError):
    """Neither scan_id nor dish_name supplied; or other domain validation."""


class FoodLogService:
    """Creates FoodLog rows with snapshotted macros."""

    def __init__(self, lookup: NutritionLookup | None = None) -> None:
        self._lookup = lookup or NutritionLookup()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, data: CreateFoodLogInput) -> FoodLog:
        if data.scan_id is None and not (data.dish_name and data.dish_name.strip()):
            raise InvalidInputError(
                "One of scan_id or dish_name must be provided"
            )

        # Idempotency check — return the prior log if the same key was
        # already used by THIS user. Cross-user key collision is
        # astronomically unlikely for UUIDs, but the user_id filter
        # makes the intent explicit and defends against caller bugs.
        if data.idempotency_key:
            prior = FoodLog.objects.filter(
                user_id=data.user_id,
                idempotency_key=data.idempotency_key,
            ).first()
            if prior is not None:
                return prior

        if data.scan_id is not None:
            return self._create_from_scan(data)
        return self._create_manual(data)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _create_from_scan(self, data: CreateFoodLogInput) -> FoodLog:
        try:
            scan = FoodScan.objects.get(id=data.scan_id, user_id=data.user_id)
        except FoodScan.DoesNotExist as exc:
            raise ScanNotOwnedError(
                f"Scan {data.scan_id} not found for user {data.user_id}"
            ) from exc

        if not scan.nutrition:
            raise DishNotRecognizedError(
                f"Scan {data.scan_id} has no nutrition snapshot — "
                "the original recognition missed and macros aren't derivable"
            )

        n = scan.nutrition
        # Scan dict may exist but per-portion totals can be null when the
        # vision provider returned a dish without a portion_g estimate
        # (NutritionFacts leaves totals=None in that branch). Snapshotting
        # would silently produce a "0 kcal" diary entry — treat as
        # not-recognized so the mobile UI prompts manual portion entry.
        if any(n.get(field) is None for field in ("kcal", "protein_g", "fat_g", "carbs_g")):
            raise DishNotRecognizedError(
                f"Scan {data.scan_id} has nutrition snapshot with missing "
                "per-portion totals — provider didn't estimate portion size"
            )

        # Scan's nutrition snapshot already contains totals at the
        # provider's portion estimate. Multiplier scales those totals.
        m = data.portion_multiplier
        return FoodLog.objects.create(
            user_id=data.user_id,
            scan=scan,
            dish_name=scan.dish_name or n.get("matched_dish", ""),
            portion_multiplier=m,
            calories=_scale(n["kcal"], m),
            protein_g=_scale(n["protein_g"], m),
            fat_g=_scale(n["fat_g"], m),
            carbs_g=_scale(n["carbs_g"], m),
            # DRF-260: micronutrient snapshot — only when scan supplied
            # them. Older scans return None across the board, which is
            # fine: pattern engine treats unknowns as low-quality data.
            **_micronutrient_snapshot_from_dict(n, m),
            meal_type=data.meal_type,
            logged_at=data.logged_at or timezone.now(),
            idempotency_key=data.idempotency_key,
        )

    def _create_manual(self, data: CreateFoodLogInput) -> FoodLog:
        assert data.dish_name is not None  # narrowed by create()
        facts = self._lookup.lookup(
            data.dish_name,
            portion_g=MANUAL_DISH_BASELINE_G,
        )
        if facts is None:
            raise DishNotRecognizedError(
                f"Dish '{data.dish_name}' not in seed and no fallback "
                "is configured (Slice 3a' will add OFF/USDA)"
            )

        m = data.portion_multiplier
        return FoodLog.objects.create(
            user_id=data.user_id,
            scan=None,
            dish_name=facts.matched_dish,
            portion_multiplier=m,
            calories=_scale(facts.kcal, m),
            protein_g=_scale(facts.protein_g, m),
            fat_g=_scale(facts.fat_g, m),
            carbs_g=_scale(facts.carbs_g, m),
            # DRF-260: snapshot micronutrients from NutritionFacts when
            # populated; defaults to None + source=unknown otherwise.
            **_micronutrient_snapshot_from_facts(facts, m),
            meal_type=data.meal_type,
            logged_at=data.logged_at or timezone.now(),
            idempotency_key=data.idempotency_key,
        )


def _scale(value: float, multiplier: float) -> float:
    """Scale a macro value by a portion multiplier.

    Caller guarantees value is non-None (partial-null snapshots are
    rejected upstream as DishNotRecognizedError).
    """
    return round(value * multiplier, 1)


# DRF-260: micronutrient snapshot helpers.
#
# Both paths (scan / manual) end up calling FoodLog.objects.create with
# eight micronutrient kwargs + ``micronutrients_source``. Centralise the
# read-and-scale logic so the two code paths can't drift.

_MICRONUTRIENT_FIELDS = (
    "vitamin_d_iu",
    "vitamin_b12_mcg",
    "vitamin_c_mg",
    "iron_mg",
    "calcium_mg",
    "magnesium_mg",
    "omega3_g",
    "fiber_g",
)


def _scale_micro(value, multiplier: float):
    if value is None:
        return None
    return round(float(value) * multiplier, 2)


def _micronutrient_snapshot_from_facts(facts, multiplier: float) -> dict:
    """Snapshot from a ``NutritionFacts`` dataclass (manual lookup path).

    NutritionFacts already exposes per-portion totals (vitamin_d_iu,
    iron_mg, ...) computed against ``portion_g`` from the seed. The
    user-supplied ``portion_multiplier`` scales those totals further —
    e.g. user logs 1.5× of a 100g baseline → multiplier=1.5.
    """
    out: dict = {"micronutrients_source": facts.micronutrients_source}
    for field in _MICRONUTRIENT_FIELDS:
        out[field] = _scale_micro(getattr(facts, field, None), multiplier)
    return out


def _micronutrient_snapshot_from_dict(nutrition: dict, multiplier: float) -> dict:
    """Snapshot from a ``FoodScan.nutrition`` JSON (scan path).

    Older scans don't carry micronutrients — every field stays None and
    ``micronutrients_source`` defaults to "unknown" so pattern engine
    correctly excludes the row from deficit-window quality math.
    """
    out: dict = {
        "micronutrients_source":
            nutrition.get("micronutrients_source") or "unknown",
    }
    for field in _MICRONUTRIENT_FIELDS:
        out[field] = _scale_micro(nutrition.get(field), multiplier)
    return out
