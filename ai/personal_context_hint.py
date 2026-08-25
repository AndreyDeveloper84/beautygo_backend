"""Render UserPersonalContext into a system-prompt advisory block (DRF-230).

The wire-up: mobile already PATCHes `/users/me/personal-context/` with
explicit preferences (districts, time slots, budget, diet, sensitivities).
Until this module landed, AI Chat never saw any of it — the LLM had to
re-ask the same questions every conversation. Plumbing the saved context
into ``render_system_prompt(extra_hint=...)`` closes the "AI который
помнит" loop for explicit signals; the deferred personalization engine
(behavioural inference, LLM extraction, anti-spam cooldowns) is Phase 6
work and not depended on here.

Why an `extra_hint` string and not a structured field on AIConcierge:

- ayla-ai-core 0.6.0's `render_system_prompt` already accepts
  `extra_hint` as a soft advisory block (added for the DRF-248
  cross-domain bridge — weekly nutrition deficits from the bot). Reusing
  that slot keeps the shared package generic; consumer-specific
  formatting (ours below) stays in Ayla.
- The block is wrapped by ayla-ai-core in "ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ
  (мягкая подсказка, не правило)" so the LLM treats it as advisory.
  That's fine for explicit preferences too — the LLM should still ask
  "это всё ещё актуально?" if a request contradicts the saved context.
- Composability with future DRF-248 hints: callers concatenate strings.
  See `ChatService._make_prompt_renderer`.

Empty contract: returns "" when no fields are set so the block is
omitted entirely (ayla-ai-core 0.6.0's renderer skips empty hints).

Происхождение факта (P0-3, `OD_C04_GROUNDED_WHY.md` §1)
-------------------------------------------------------
Модель обязана отличать «человек это ввёл» от «мы это вывели». Строка
`UserPersonalContext.data_sources` хранит источник по полю; этот
рендерер его не читал и печатал одни значения.

Сегодня прямого вреда здесь нет, и это стоит записать честно: единственные
поля, которые ставит ночной inference (`users/personal_context_inference.py`)
— `favorite_masters` и `busy_days` — этот блок вообще не рендерит. Но
`_SOURCE_CHOICES` внутреннего PATCH принимает `behavioral`/`conversational`/
`transactional` для ЛЮБОГО зелёного поля, так что дыра открыта, просто в неё
пока никто не пролез. Отмечаем сейчас, пока пометка ничего не стоит:
при всех `explicit` вывод байт-в-байт прежний.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from users.models import UserPersonalContext


__all__ = ["format_personal_context_hint"]


_TIME_SLOT_LABELS: dict[str, str] = {
    "early_morning": "до 9 утра",
    "morning": "утро (9–12)",
    "afternoon": "день (12–17)",
    "evening": "вечер (17–21)",
    "late_evening": "после 21",
}

# Пометка выведенного значения. Ставится ПЕРЕД содержимым строки, чтобы
# граница пережила усечение списка. Значение `data_sources[field]`, равное
# «человек ввёл сам»; всё остальное (и незнакомое) — вывод системы.
_STATED_SOURCE = "explicit"
_INFERRED_MARK = "(вывод, клиент этого не говорил)"

_DIET_LABELS: dict[str, str] = {
    "omnivore": "всеядное",
    "vegetarian": "вегетарианство",
    "vegan": "веганство",
    "keto": "кето",
    "halal": "халяль",
    "kosher": "кошер",
    "other": "особая диета",
}


def format_personal_context_hint(
    personal_context: "UserPersonalContext | None",
) -> str:
    """Render the user's saved preferences as a Russian bullet list.

    Returns an empty string for no context / no fields set — the caller
    can pass it straight to ``render_system_prompt(extra_hint=...)``
    without checking, and the prompt renderer skips the block.

    Field selection mirrors the model's current shape (DRF-174 reduced).
    Adding a field here later is backward-compatible: missing fields
    just don't render.
    """
    if personal_context is None:
        return ""

    # getattr: callers duck-type this object (tests, and the ChatService
    # seam), and a missing map must mean «no marking», never a crash.
    sources = getattr(personal_context, "data_sources", None) or {}

    def bullet(field: str, body: str) -> str:
        """One bullet, marked when the value is a derivation, not a statement."""
        if sources.get(field, _STATED_SOURCE) == _STATED_SOURCE:
            return f"- {body}"
        return f"- {_INFERRED_MARK} {body}"

    lines: list[str] = []

    districts = list(personal_context.preferred_districts or [])
    if districts:
        # Cap at 5 to keep the prompt token-budget tight — districts are
        # the noisiest field (mobile lets users multi-select up to 20).
        shown = districts[:5]
        suffix = (
            f" (+ещё {len(districts) - 5})" if len(districts) > 5 else ""
        )
        lines.append(
            bullet(
                "preferred_districts",
                f"предпочитаемые районы: {', '.join(shown)}{suffix}",
            )
        )

    time_slots = list(personal_context.preferred_time_slots or [])
    if time_slots:
        labels = [
            _TIME_SLOT_LABELS.get(slot, slot) for slot in time_slots
        ]
        lines.append(bullet("preferred_time_slots", f"удобное время: {', '.join(labels)}"))

    lo = personal_context.price_range_min
    hi = personal_context.price_range_max
    if lo is not None and hi is not None:
        lines.append(bullet("price_range_min", f"бюджет: {_money(lo)}–{_money(hi)} ₽"))
    elif lo is not None:
        lines.append(bullet("price_range_min", f"бюджет: от {_money(lo)} ₽"))
    elif hi is not None:
        lines.append(bullet("price_range_max", f"бюджет: до {_money(hi)} ₽"))

    diet = personal_context.diet_type or ""
    if diet:
        lines.append(bullet("diet_type", f"диета: {_DIET_LABELS.get(diet, diet)}"))

    sensitivities = list(personal_context.skin_sensitivities or [])
    if sensitivities:
        shown = sensitivities[:8]
        suffix = (
            f" (+ещё {len(sensitivities) - 8})"
            if len(sensitivities) > 8
            else ""
        )
        lines.append(
            bullet(
                "skin_sensitivities",
                f"чувствительность / аллергии: {', '.join(shown)}{suffix}",
            )
        )

    if personal_context.prefers_flexible_cancellation:
        lines.append(
            bullet(
                "prefers_flexible_cancellation",
                "ценит гибкую политику отмены (предлагай мастеров с такой)",
            )
        )

    if not lines:
        return ""

    return "ИЗВЕСТНЫЕ ПРЕДПОЧТЕНИЯ КЛИЕНТА:\n" + "\n".join(lines)


def _money(value) -> str:
    """Decimal → human-readable, no trailing zeros for round amounts."""
    # Decimal('2000.00') → '2000', Decimal('2500.50') → '2500.50'.
    s = f"{value:.2f}".rstrip("0").rstrip(".")
    return s or "0"
