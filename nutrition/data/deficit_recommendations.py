"""Mapping deficit signal → soft prompt-hint for AIConcierge (DRF-248).

Pure-Python module (not YAML) so we don't add a runtime dep. Hints are
deliberately soft / optional — they tell the LLM to mention the topic
"if appropriate" so an unrelated question (e.g. «во сколько работаете?»)
isn't derailed into food talk.

Editing rules:
- Keep claims general wellness, not medical. We never say "массаж лечит
  дефицит белка" — we say "после нагрузок белок важен" / "релакс полезен".
- Hint must end with a soft modifier («если уместно», «по случаю»).
- One trigger → one hint. Multiple triggers compose by the caller.

Adding a new signal:
1. Add a row to RULES with a unique key.
2. Implement the predicate over WeeklyDeficits in deficit_hints.py.
3. Add tests covering trigger/no-trigger boundary.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HintRule:
    """One deficit→hint mapping. ``key`` is stable wire-id for analytics."""

    key: str
    hint_template: str


# Soft, optional, non-medical. Templates are .format()-ed with kwargs from
# the deficit signal (e.g. ``streak``, ``pct``).
RULES: dict[str, HintRule] = {
    "protein_low_streak": HintRule(
        key="protein_low_streak",
        hint_template=(
            "У клиента уже {streak} дн. подряд белок ниже нормы. "
            "Если он сам спросит про восстановление, спорт или массаж — "
            "по случаю можно мягко упомянуть, что белок особенно важен после "
            "нагрузок. Никаких медицинских рекомендаций, не настаивай."
        ),
    ),
}
