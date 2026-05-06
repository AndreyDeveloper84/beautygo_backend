"""Nutrition lookup — dish name → macros.

Slice 3a ships a seed-only lookup against the hand-curated RU dishes
table in ``nutrition.data.ru_dishes_seed``. Slice 3a' (planned) adds
Open Food Facts / USDA HTTP fallback for dishes not in the seed; the
``Source`` enum and the ``source`` field on ``NutritionFacts`` are
already shaped for that fallback so the consumer doesn't change when
HTTP arrives.

Why a service object and not a free function:
- Future Slice 3a' fallback chain (seed → OFF → USDA) needs config and
  caching; constructor is the natural place for it.
- Tests can swap the seed for a tiny fixture without monkey-patching
  module globals.

Match strategy:
1. Normalize the input name (lowercase, ё→е, strip punctuation/spaces)
2. Direct hit on ``DISH_MACROS`` keys
3. Direct hit on ``ALIASES`` (alias → canonical → macros)
4. Substring match against ingredients list (last resort, only when
   provider returned an unknown dish but recognisable ingredients —
   e.g. "блюдо с курицей и рисом" → пытаемся "рис")
5. Otherwise None

Substring matching is intentionally narrow (exact tokens after
normalization) to avoid false positives like "сырник" matching "сыр".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Iterable

from nutrition.data.ru_dishes_seed import ALIASES, DISH_MACROS, DishMacros


# ---------------------------------------------------------------------------
# Public DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NutritionFacts:
    """Result of a nutrition lookup.

    Per-100g values are always populated. Per-portion totals are
    populated only when ``portion_g`` is known — otherwise None and
    the mobile client shows "укажите порцию" with the per-100g hint.

    DRF-260 adds 8 optional micronutrients (per-100g + per-portion) plus
    a ``micronutrients_source`` tag. Defaults to None / "unknown" so the
    existing macro-only call sites stay valid; the cross-domain rule
    engine reads these fields when they're populated.
    """

    matched_dish: str
    source: str                       # "seed_ru" / "off" / "usda"
    portion_g: float | None
    kcal_per_100g: float
    protein_g_per_100g: float
    fat_g_per_100g: float
    carbs_g_per_100g: float
    kcal: float | None                # totals for this portion
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None

    # DRF-260: micronutrients per 100g.
    vitamin_d_iu_per_100g: float | None = None
    vitamin_b12_mcg_per_100g: float | None = None
    vitamin_c_mg_per_100g: float | None = None
    iron_mg_per_100g: float | None = None
    calcium_mg_per_100g: float | None = None
    magnesium_mg_per_100g: float | None = None
    omega3_g_per_100g: float | None = None
    fiber_g_per_100g: float | None = None

    # DRF-260: per-portion totals (None when portion_g is None).
    vitamin_d_iu: float | None = None
    vitamin_b12_mcg: float | None = None
    vitamin_c_mg: float | None = None
    iron_mg: float | None = None
    calcium_mg: float | None = None
    magnesium_mg: float | None = None
    omega3_g: float | None = None
    fiber_g: float | None = None

    # DRF-260: provenance tag — read by Track E pattern engine.
    micronutrients_source: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, normalize ё→е, collapse whitespace.

    Hyphens are preserved because ``котлета по-киевски`` carries one;
    everything else gets squashed.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text).lower()
    s = s.replace("ё", "е")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class NutritionLookup:
    """Looks up macros for a dish name.

    MVP: seed-only (``nutrition.data.ru_dishes_seed``). HTTP fallback
    arrives in Slice 3a'.
    """

    SOURCE_SEED = "seed_ru"

    def __init__(
        self,
        *,
        dish_macros: dict[str, DishMacros] | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._dish_macros = dish_macros if dish_macros is not None else DISH_MACROS
        self._aliases = aliases if aliases is not None else ALIASES

    def lookup(
        self,
        dish_name: str,
        *,
        ingredients: Iterable[str] = (),
        portion_g: float | None = None,
    ) -> NutritionFacts | None:
        """Return facts for ``dish_name``, or None if not in any source."""
        canonical = self._resolve_canonical(dish_name, ingredients)
        if canonical is None:
            return None
        macros = self._dish_macros[canonical]
        return self._build_facts(canonical, macros, portion_g)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_canonical(
        self, dish_name: str, ingredients: Iterable[str],
    ) -> str | None:
        norm = _normalize(dish_name)
        if not norm:
            return self._match_ingredients(ingredients)

        # 1. Direct canonical match
        if norm in self._dish_macros:
            return norm

        # 2. Alias match
        if norm in self._aliases:
            return self._aliases[norm]

        # 3. Token-prefix match — handle "борщ московский" → "борщ".
        # Try the first token alone (most provider responses lead with
        # the canonical noun) before giving up.
        tokens = norm.split()
        if tokens and tokens[0] in self._dish_macros:
            return tokens[0]
        if tokens and tokens[0] in self._aliases:
            return self._aliases[tokens[0]]

        # 4. Ingredients fallback
        return self._match_ingredients(ingredients)

    def _match_ingredients(self, ingredients: Iterable[str]) -> str | None:
        """Last-resort ingredient lookup.

        Only fires when the dish name itself didn't resolve. We match
        each ingredient as a *whole token* against the canonical dish
        keys — so "рис" matches "рис" but "сыр" doesn't accidentally
        match a dish whose key contains "сыр" as a substring.
        """
        for ing in ingredients:
            n = _normalize(ing)
            if not n:
                continue
            if n in self._dish_macros:
                return n
            if n in self._aliases:
                return self._aliases[n]
        return None

    def _build_facts(
        self, canonical: str, macros: DishMacros, portion_g: float | None,
    ) -> NutritionFacts:
        if portion_g is None or portion_g <= 0:
            kcal = protein = fat = carbs = None
            ratio = None
        else:
            ratio = portion_g / 100.0
            kcal = round(macros.kcal_per_100g * ratio, 1)
            protein = round(macros.protein_g_per_100g * ratio, 1)
            fat = round(macros.fat_g_per_100g * ratio, 1)
            carbs = round(macros.carbs_g_per_100g * ratio, 1)

        # DRF-260: scale micronutrients the same way macros are scaled.
        # ``ratio`` is None when portion_g is unknown — totals stay None
        # too, and the mobile client falls back to per-100g hints.
        def _scale_micro(value: float | None) -> float | None:
            if value is None or ratio is None:
                return None
            return round(value * ratio, 2)

        return NutritionFacts(
            matched_dish=canonical,
            source=self.SOURCE_SEED,
            portion_g=portion_g if (portion_g and portion_g > 0) else None,
            kcal_per_100g=macros.kcal_per_100g,
            protein_g_per_100g=macros.protein_g_per_100g,
            fat_g_per_100g=macros.fat_g_per_100g,
            carbs_g_per_100g=macros.carbs_g_per_100g,
            kcal=kcal,
            protein_g=protein,
            fat_g=fat,
            carbs_g=carbs,
            # Per-100g micronutrients pass through verbatim.
            vitamin_d_iu_per_100g=macros.vitamin_d_iu_per_100g,
            vitamin_b12_mcg_per_100g=macros.vitamin_b12_mcg_per_100g,
            vitamin_c_mg_per_100g=macros.vitamin_c_mg_per_100g,
            iron_mg_per_100g=macros.iron_mg_per_100g,
            calcium_mg_per_100g=macros.calcium_mg_per_100g,
            magnesium_mg_per_100g=macros.magnesium_mg_per_100g,
            omega3_g_per_100g=macros.omega3_g_per_100g,
            fiber_g_per_100g=macros.fiber_g_per_100g,
            # Per-portion totals scaled by ratio.
            vitamin_d_iu=_scale_micro(macros.vitamin_d_iu_per_100g),
            vitamin_b12_mcg=_scale_micro(macros.vitamin_b12_mcg_per_100g),
            vitamin_c_mg=_scale_micro(macros.vitamin_c_mg_per_100g),
            iron_mg=_scale_micro(macros.iron_mg_per_100g),
            calcium_mg=_scale_micro(macros.calcium_mg_per_100g),
            magnesium_mg=_scale_micro(macros.magnesium_mg_per_100g),
            omega3_g=_scale_micro(macros.omega3_g_per_100g),
            fiber_g=_scale_micro(macros.fiber_g_per_100g),
            micronutrients_source=macros.micronutrients_source,
        )
