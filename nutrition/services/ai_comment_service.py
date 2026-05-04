"""AI daily-summary comment generator (DRF-303 §4.2).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §4.2.
Acceptance: docs/plans/maxbot-phase3-linear-issues.md DRF-303.

Two paths:
- ``eating_disorder=true`` → return a fixed supportive template with no
  numeric calorie cues. Never calls the LLM.
- otherwise → gpt-4o-mini, tone tied to ``goal`` + relevant
  ``health_flags``. Output validated to ≤3 sentences, ≤220 chars; on
  validation failure we re-prompt once with an explicit length warning
  and fall back to a neutral template if that also fails.

Server-side cache: 6 hours per user-per-day in Django's cache framework.
The bot's ``/день`` button can fire repeatedly without re-spending LLM
tokens.

Cost guard: an in-process counter logs ``ai_comment.daily_cost_warning``
when daily LLM calls exceed ``settings.NUTRITION_AI_COMMENT_DAILY_LIMIT``
(default 1000 — about $30/day at gpt-4o-mini list price). The hard
budget cap is left to the platform billing alert; this is just a tripwire.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_tz

from django.conf import settings
from django.core.cache import cache

from nutrition.models import NutritionProfile

logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 6 * 60 * 60
COMMENT_MAX_CHARS = 220
COMMENT_MAX_SENTENCES = 3

# Count entries are bumped per LLM call; key includes UTC date so the
# counter resets at midnight without a separate cleanup task.
_COST_KEY_PREFIX = "nutrition.ai_comment.daily_count"
_DEFAULT_DAILY_LIMIT = 1000


@dataclass(frozen=True)
class SummaryFacts:
    """Trimmed view of NutritionSummary used by the comment template.

    Only the fields the LLM needs — keeps prompt small and the contract
    boring. Not a Pydantic model on purpose; this is internal.
    """
    calories_total: float
    calories_goal: int
    protein_g: float
    fat_g: float
    carbs_g: float
    water_ml: int
    water_goal_ml: int
    entries_count: int


# ---------------------------------------------------------------------------
# Eating-disorder template (no numbers — spec §10)
# ---------------------------------------------------------------------------


_ED_TEMPLATES = (
    "Как ты сегодня? День получился — это уже важно. "
    "Если хочется — поделись, как себя чувствуешь.",
    "Сегодня ты заботилась о себе — это и есть результат. "
    "Расскажешь, как настроение?",
    "Ты пришла сюда — это уже шаг. Завтра поговорим снова, "
    "если будет нужно.",
)


def _eating_disorder_comment(day: date) -> str:
    return _ED_TEMPLATES[day.toordinal() % len(_ED_TEMPLATES)]


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class AICommentService:
    """Stateless. Caching + LLM are pluggable for tests."""

    def __init__(self, llm_client_factory=None):
        # Lazy import to keep test runtime free of OpenAI SDK overhead
        # when the suite never reaches the live path.
        if llm_client_factory is None:
            from ai.services.llm_client import get_openai_client
            llm_client_factory = get_openai_client
        self._llm_client_factory = llm_client_factory

    def comment_for(
        self,
        *,
        user_id: int,
        day: date,
        facts: SummaryFacts,
    ) -> str:
        cache_key = _cache_key(user_id, day)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        profile = NutritionProfile.objects.filter(user_id=user_id).first()
        flags = (profile.health_flags or {}) if profile else {}

        if flags.get("eating_disorder"):
            comment = _eating_disorder_comment(day)
            cache.set(cache_key, comment, CACHE_TTL_SECONDS)
            return comment

        comment = self._llm_comment(profile, facts)
        cache.set(cache_key, comment, CACHE_TTL_SECONDS)
        return comment

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _llm_comment(
        self, profile: NutritionProfile | None, facts: SummaryFacts,
    ) -> str:
        prompt = _build_prompt(profile, facts)
        try:
            text = self._call_llm(prompt)
        except Exception as exc:  # noqa: BLE001 — broad: any LLM failure → fallback
            logger.warning("nutrition.ai_comment.llm_error err=%s", exc)
            return _neutral_fallback(facts)

        validated = _validate(text)
        if validated is not None:
            return validated

        # One re-prompt with an explicit length warning, then fall back.
        try:
            text2 = self._call_llm(prompt + "\n\nКОРОТКО: 3 предложения, ≤220 символов.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("nutrition.ai_comment.llm_retry_error err=%s", exc)
            return _neutral_fallback(facts)
        validated = _validate(text2)
        return validated if validated is not None else _neutral_fallback(facts)

    def _call_llm(self, prompt: str) -> str:
        _bump_cost_counter()
        client = self._llm_client_factory()
        completion = client.chat.completions.create(
            model=getattr(settings, "NUTRITION_AI_COMMENT_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=180,
            temperature=0.6,
            timeout=10.0,
        )
        return (completion.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
Ты — поддерживающий диетолог-консультант. Пишешь короткий комментарий
к дневному отчёту по питанию: 1-3 предложения, не более 220 символов.
Тон: тёплый, уважительный, без упрёков, без слова «диета».
Никогда не давай медицинских советов. Не предлагай добавки и не
ставь диагнозов.
"""


def _build_prompt(profile: NutritionProfile | None, facts: SummaryFacts) -> str:
    goal = (profile.goal if profile else "") or "maintain"
    flags = (profile.health_flags or {}) if profile else {}

    tone_hint = {
        "lose": "мягко поддержи дефицит, отметь белок и воду как опору",
        "gain": "поддержи прибавление, отметь белок и общий калораж",
        "tone": "сфокусируйся на белке, фокус на формулу и настроение",
        "maintain": "нейтральный поддерживающий тон, без целей по весу",
    }.get(goal, "нейтральный поддерживающий тон")

    flag_notes = []
    if flags.get("pregnant"):
        flag_notes.append("беременность — никаких ограничивающих советов")
    if flags.get("breastfeeding"):
        flag_notes.append("ГВ — поощряй белок и воду")
    if flags.get("diabetes_t1") or flags.get("diabetes_t2") or flags.get("prediabetes"):
        flag_notes.append("диабет — без рекомендаций по сахару, без цифр углеводов")
    if flags.get("hypertension"):
        flag_notes.append("гипертония — без советов по натрию")
    if flags.get("gi_problems"):
        flag_notes.append("ЖКТ — мягкий тон, без острых советов")

    flags_block = (
        "Учти: " + "; ".join(flag_notes) + "."
        if flag_notes else ""
    )

    return (
        f"Цель пользователя: {goal} (тон: {tone_hint}).\n"
        f"{flags_block}\n\n"
        f"Факты дня:\n"
        f"- калории: {int(facts.calories_total)} из {facts.calories_goal} цели\n"
        f"- белок: {int(facts.protein_g)} г\n"
        f"- жиры: {int(facts.fat_g)} г\n"
        f"- углеводы: {int(facts.carbs_g)} г\n"
        f"- вода: {facts.water_ml} из {facts.water_goal_ml} мл\n"
        f"- записей в дневнике: {facts.entries_count}\n\n"
        f"Напиши 1-3 предложения, ≤220 символов, на русском, без цифр в "
        f"финале (число — только если естественно ложится в фразу)."
    )


# ---------------------------------------------------------------------------
# Validation + fallbacks
# ---------------------------------------------------------------------------


def _validate(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if len(text) > COMMENT_MAX_CHARS:
        return None
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", text) if s]
    if len(sentences) > COMMENT_MAX_SENTENCES:
        return None
    return text


def _neutral_fallback(facts: SummaryFacts) -> str:
    if facts.entries_count == 0:
        return "Сегодня записей пока нет — попробуем завтра. Я рядом."
    if facts.water_ml < facts.water_goal_ml * 0.7:
        return "Хороший день. Завтра попробуй чуть больше воды — это поддержит самочувствие."
    return "Хороший день — почти в норме. Завтра попробуй немного больше белка с утра."


def _cache_key(user_id: int, day: date) -> str:
    return f"nutrition.ai_comment:{user_id}:{day.isoformat()}"


def _bump_cost_counter() -> int:
    today = datetime.now(dt_tz.utc).date().isoformat()
    key = f"{_COST_KEY_PREFIX}:{today}"
    try:
        new_count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 60 * 60 * 26)
        new_count = 1

    limit = int(getattr(
        settings, "NUTRITION_AI_COMMENT_DAILY_LIMIT", _DEFAULT_DAILY_LIMIT,
    ))
    if new_count == limit + 1:
        # Fire once at crossing, not on every subsequent call.
        logger.warning(
            "nutrition.ai_comment.daily_cost_warning count=%d limit=%d",
            new_count, limit,
        )
    return new_count
