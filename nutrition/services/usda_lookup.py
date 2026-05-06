"""USDA FoodData Central lookup with local cache (DRF-261).

Wraps the public ``/v1/foods/search`` endpoint of FoodData Central with
two safety layers:

1. **Local cache** — every successful lookup writes a ``USDAFoodCache``
   row keyed by the normalized search term. Re-lookups skip HTTP. The
   1000 req/hour API quota is the hard ceiling we never want to hit.

2. **Bounded failure modes** — network timeouts and 5xx propagate as
   ``USDAUnavailableError`` so the lookup chain can fall through to AI
   estimation. A clean miss (200 with empty ``foods`` array) returns
   ``None`` — *not* an error — so the chain treats it as "USDA didn't
   know this dish" rather than "USDA is broken".

The parser keeps only the eight Track E micronutrients + four macros;
the rest of the USDA nutrient zoo is dropped. This is a deliberate
narrowing — Track E has no use for, e.g., ``Lutein + zeaxanthin`` even
if USDA reports it.

Output shape: a ``NutritionFacts`` dataclass. Per-100g values come
straight from USDA (their ``foodNutrients[].value`` is per 100g for
foundation/SR-legacy foods). Per-portion values are scaled when
``portion_g`` is supplied.
"""
from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings

from nutrition.models import USDAFoodCache
from nutrition.services.nutrition_lookup import NutritionFacts


USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_TIMEOUT_S = 5.0


# USDA nutrientId → (NutritionFacts attribute, unit-conversion factor)
# Conversion factor maps USDA's stored unit to NutritionFacts' unit.
# Most are 1.0 (USDA already reports MG / G / IU); we only need to flag
# the ones where USDA's unit name differs from ours.
_NUTRIENT_MAP: dict[int, tuple[str, float]] = {
    1008: ("kcal_per_100g", 1.0),                    # Energy KCAL
    1003: ("protein_g_per_100g", 1.0),               # Protein G
    1004: ("fat_g_per_100g", 1.0),                   # Total lipid (fat) G
    1005: ("carbs_g_per_100g", 1.0),                 # Carbohydrate G
    1110: ("vitamin_d_iu_per_100g", 1.0),            # Vitamin D IU
    1178: ("vitamin_b12_mcg_per_100g", 1.0),         # Vitamin B-12 UG
    1162: ("vitamin_c_mg_per_100g", 1.0),            # Vitamin C MG
    1089: ("iron_mg_per_100g", 1.0),                 # Iron, Fe MG
    1087: ("calcium_mg_per_100g", 1.0),              # Calcium, Ca MG
    1090: ("magnesium_mg_per_100g", 1.0),            # Magnesium, Mg MG
    # USDA reports omega-3 split across multiple nutrients; we sum them
    # in _parse_food. Listing the IDs we care about here:
    1404: ("omega3_g_per_100g", 1.0),                # 18:3 n-3 c,c,c (ALA) G
    1405: ("omega3_g_per_100g", 1.0),                # 20:5 n-3 (EPA) G
    1406: ("omega3_g_per_100g", 1.0),                # 22:6 n-3 (DHA) G
    1079: ("fiber_g_per_100g", 1.0),                 # Fiber, total dietary G
}

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


class USDAUnavailableError(Exception):
    """USDA API is down (timeout or 5xx). Caller should fall through to AI."""


def _normalize(term: str) -> str:
    return term.strip().casefold()


class USDALookup:
    """USDA FoodData Central client with local cache."""

    SOURCE_USDA = "usda"

    def __init__(self) -> None:
        self._api_key = getattr(settings, "USDA_API_KEY", "") or ""
        if not self._api_key:
            raise ValueError(
                "USDA_API_KEY is empty — refusing to start lookup with "
                "no credentials. Set settings.USDA_API_KEY or skip this layer.",
            )

    def lookup(
        self, dish_name: str, *, portion_g: float | None = None,
    ) -> NutritionFacts | None:
        """Return a NutritionFacts for ``dish_name`` or ``None`` on miss.

        Cache lookup happens first; HTTP is only called on cache miss.
        On HTTP failure, raises ``USDAUnavailableError`` — never returns
        a partially-filled result.
        """
        key = _normalize(dish_name)
        if not key:
            return None

        cached = self._read_cache(key)
        if cached is not None:
            return self._build_facts(cached, portion_g)

        parsed = self._fetch(key)
        if parsed is None:
            return None

        self._write_cache(key, parsed)
        return self._build_facts(parsed, portion_g)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _read_cache(self, key: str) -> dict | None:
        # DRF-261 LB-1 fix: search_key stays unique per row, but multiple
        # normalized terms can resolve to the same fdc_id. We accumulate
        # aliases in data["search_aliases"] and check them in Python —
        # JSON __contains is not portable across SQLite/Postgres backends.
        row = USDAFoodCache.objects.filter(search_key=key).first()
        if row:
            return row.data
        for candidate in USDAFoodCache.objects.only("data").iterator():
            aliases = (candidate.data or {}).get("search_aliases") or []
            if key in aliases:
                return candidate.data
        return None

    def _write_cache(self, key: str, parsed: dict) -> None:
        # DRF-261 LB-1 fix: NEVER overwrite search_key on existing
        # cache row. Earlier behavior caused alias drift — a second
        # search term resolving to the same fdc_id would clobber the
        # first term, breaking subsequent reads of the original key.
        #
        # Instead: get_or_create keeps search_key immutable for an
        # existing row; we accumulate aliases in data["search_aliases"]
        # so future reads find the row via either column.
        row, created = USDAFoodCache.objects.get_or_create(
            fdc_id=parsed["fdc_id"],
            defaults={
                "description": parsed["description"],
                "search_key": key,
                "data": {**parsed, "search_aliases": [key]},
            },
        )
        if not created and key not in (row.data.get("search_aliases") or []):
            # Append alias to existing row without touching search_key.
            aliases = list(row.data.get("search_aliases") or [])
            aliases.append(key)
            row.data = {**parsed, "search_aliases": aliases}
            row.save(update_fields=["data", "updated_at"])

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch(self, key: str) -> dict | None:
        body = {"query": key, "pageSize": 1, "dataType": ["Foundation", "SR Legacy"]}
        params = {"api_key": self._api_key}
        try:
            with httpx.Client(timeout=USDA_TIMEOUT_S) as client:
                response = client.post(USDA_SEARCH_URL, json=body, params=params)
        except httpx.TimeoutException as exc:
            raise USDAUnavailableError(f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise USDAUnavailableError(f"http: {exc}") from exc

        # DRF-261 LB-2 fix: classify auth + rate-limit errors loudly
        # so we don't silently bleed money into the AI estimator
        # fallback when our API key breaks or we exceed quota.
        if response.status_code in (401, 403):
            raise USDAUnavailableError(
                f"auth error {response.status_code} — check USDA_API_KEY",
            )
        if response.status_code == 429:
            raise USDAUnavailableError(
                f"rate limit {response.status_code} — quota exceeded",
            )
        if response.status_code >= 500:
            raise USDAUnavailableError(f"5xx: {response.status_code}")

        # Other 4xx: caller bug or malformed query — treat as miss.
        if response.status_code >= 400:
            return None

        payload = response.json()
        foods = payload.get("foods") or []
        if not foods:
            return None

        return self._parse_food(foods[0])

    # ------------------------------------------------------------------
    # Parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_food(food: dict) -> dict:
        """Extract the macros + 8 micronutrients we care about.

        USDA's ``foodNutrients`` is an array of {nutrientId, value, ...}
        objects per 100g. We sum omega-3 components (ALA + EPA + DHA)
        and pass everything else through verbatim.
        """
        out: dict[str, Any] = {
            "fdc_id": food["fdcId"],
            "description": food["description"],
        }
        omega3_total: float | None = None
        seen_micros: set[str] = set()

        for nut in food.get("foodNutrients") or []:
            nut_id = nut.get("nutrientId")
            if nut_id not in _NUTRIENT_MAP:
                continue
            attr, factor = _NUTRIENT_MAP[nut_id]
            val = nut.get("value")
            if val is None:
                continue
            scaled = float(val) * factor

            if attr == "omega3_g_per_100g":
                # Sum ALA + EPA + DHA into one omega-3 total.
                omega3_total = (omega3_total or 0.0) + scaled
            else:
                out[attr] = scaled
            # Track micronutrient coverage for the source tag.
            base = attr.replace("_per_100g", "")
            if base in _MICRONUTRIENT_FIELDS:
                seen_micros.add(base)

        if omega3_total is not None:
            out["omega3_g_per_100g"] = round(omega3_total, 3)
            seen_micros.add("omega3_g")

        # Source tag based on micronutrient coverage. ≥6/8 → full.
        out["micronutrients_source"] = (
            "usda_full" if len(seen_micros) >= 6 else "usda_partial"
        )
        return out

    # ------------------------------------------------------------------
    # Build NutritionFacts
    # ------------------------------------------------------------------

    @staticmethod
    def _build_facts(parsed: dict, portion_g: float | None) -> NutritionFacts:
        ratio = (portion_g / 100.0) if (portion_g and portion_g > 0) else None

        def _scale(per_100g):
            if per_100g is None or ratio is None:
                return None
            return round(per_100g * ratio, 2)

        kcal_p100 = parsed.get("kcal_per_100g") or 0.0
        protein_p100 = parsed.get("protein_g_per_100g") or 0.0
        fat_p100 = parsed.get("fat_g_per_100g") or 0.0
        carbs_p100 = parsed.get("carbs_g_per_100g") or 0.0

        return NutritionFacts(
            matched_dish=parsed["description"],
            source=USDALookup.SOURCE_USDA,
            portion_g=portion_g if (portion_g and portion_g > 0) else None,
            kcal_per_100g=kcal_p100,
            protein_g_per_100g=protein_p100,
            fat_g_per_100g=fat_p100,
            carbs_g_per_100g=carbs_p100,
            kcal=_scale(kcal_p100),
            protein_g=_scale(protein_p100),
            fat_g=_scale(fat_p100),
            carbs_g=_scale(carbs_p100),
            vitamin_d_iu_per_100g=parsed.get("vitamin_d_iu_per_100g"),
            vitamin_b12_mcg_per_100g=parsed.get("vitamin_b12_mcg_per_100g"),
            vitamin_c_mg_per_100g=parsed.get("vitamin_c_mg_per_100g"),
            iron_mg_per_100g=parsed.get("iron_mg_per_100g"),
            calcium_mg_per_100g=parsed.get("calcium_mg_per_100g"),
            magnesium_mg_per_100g=parsed.get("magnesium_mg_per_100g"),
            omega3_g_per_100g=parsed.get("omega3_g_per_100g"),
            fiber_g_per_100g=parsed.get("fiber_g_per_100g"),
            vitamin_d_iu=_scale(parsed.get("vitamin_d_iu_per_100g")),
            vitamin_b12_mcg=_scale(parsed.get("vitamin_b12_mcg_per_100g")),
            vitamin_c_mg=_scale(parsed.get("vitamin_c_mg_per_100g")),
            iron_mg=_scale(parsed.get("iron_mg_per_100g")),
            calcium_mg=_scale(parsed.get("calcium_mg_per_100g")),
            magnesium_mg=_scale(parsed.get("magnesium_mg_per_100g")),
            omega3_g=_scale(parsed.get("omega3_g_per_100g")),
            fiber_g=_scale(parsed.get("fiber_g_per_100g")),
            micronutrients_source=parsed.get("micronutrients_source", "unknown"),
        )
