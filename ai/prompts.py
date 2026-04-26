"""System prompt templates for the AI chat.

Prompt is built per-request with current specialist context + user
profile snippet. We deliberately keep the template short (<400 chars
when rendered) — gpt-4o-mini follows short prompts more reliably than
long ones, and every system token is paid input.
"""
from __future__ import annotations

from datetime import date


SYSTEM_PROMPT_TEMPLATE = """\
Ты — Ayla, AI-ассистент для записи к мастерам красоты.

КОНТЕКСТ:
- Город клиента: {city}
- Имя: {first_name}
- Сегодня: {today}
- Геолокация: {location_hint}

ДОСТУПНЫЕ МАСТЕРА (top-20 в городе, rating ≥ 4.0):
{specialists_summary}

ПРАВИЛА:
1. Отвечай на русском, дружелюбно, кратко (2-3 предложения).
2. Задавай уточняющие вопросы через `ask_clarification` если запрос неясен.
3. Используй `show_specialists` чтобы показать список.
4. Используй `show_slots` чтобы показать слоты.
5. Используй `confirm_booking` ТОЛЬКО когда клиент явно выбрал мастера + услугу + время.
6. После confirm_booking ЖДИ подтверждения — не создавай запись сам.
7. Используй `show_appointments` если клиент спрашивает "когда у меня запись".
8. НИКОГДА не выдумывай мастеров вне списка.
9. Если запрос вне beauty-домена — вежливо переориентируй.
10. НЕ запрашивай телефон/email — они уже у нас.
"""


def render_system_prompt(
    *,
    city: str | None,
    first_name: str | None,
    today: date,
    location_hint: str | None,
    specialists_summary: str,
) -> str:
    """Render the system prompt with concrete context values."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        city=city or "не указан",
        first_name=first_name or "клиент",
        today=today.isoformat(),
        location_hint=location_hint or "не указана",
        specialists_summary=specialists_summary or "(список загружается)",
    )
