"""Build prompt hints from WeeklyDeficits — soft cross-domain bridge (DRF-248).

Pure function: WeeklyDeficits + settings → (hint_str, fired_keys). Hint is
the empty string when no signals trigger; caller (InternalDeficitsView)
should still return a 200 with empty hint so the bot can deterministically
plumb through whether or not anything fired.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from nutrition.data.deficit_recommendations import RULES
from nutrition.services.nutrition_summary_service import WeeklyDeficits


@dataclass(frozen=True)
class DeficitHint:
    """Result returned from build_deficit_hint.

    ``fired_keys`` lets analytics distinguish "no data" from "data but no
    trigger" without re-scanning the deficit signals on the caller side.
    """

    hint: str
    fired_keys: list[str]


def build_deficit_hint(deficits: WeeklyDeficits) -> DeficitHint:
    """Apply each rule's predicate to the deficit signal; concat fired hints."""
    fired: list[str] = []
    parts: list[str] = []

    min_streak = int(getattr(settings, "FOOD_DEFICIT_MIN_STREAK_DAYS", 3) or 0)
    if min_streak > 0 and deficits.protein_low_streak_days >= min_streak:
        rule = RULES["protein_low_streak"]
        parts.append(rule.hint_template.format(
            streak=deficits.protein_low_streak_days,
        ))
        fired.append(rule.key)

    return DeficitHint(hint=" ".join(parts), fired_keys=fired)
