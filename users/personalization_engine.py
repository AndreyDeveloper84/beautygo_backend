"""DRF-230 PR 3 — PersonalizationEngine.

Anti-spam decision layer between the AI Concierge / mobile UI and the
``UserPersonalContext`` write path. Callers ask the engine "may I
prompt for ``workplace_district``?" — the engine consults the user's
state and returns a verdict + a reason code suitable for telemetry.

Eight rules per CLAUDE.md / Notion 334b0dab295581d587cfeaf49efd2d5b:

1. **One field per session** — caller responsibility (we expose
   ``mark_asked`` and the helper ``recently_asked`` count). Engine
   doesn't track session boundaries; it does the DB-side checks.
2. **Not on first interaction** — block until ``onboarding_completed``.
3. **24h cooldown** — last_asked_at[field] + 24h must be in the past.
4. **Skip × 2 → 30-day pause** — skipped_questions[field].count >= 2
   AND last_at within 30 days.
5. **Already have data → silent** — ``explicit`` silences the question
   (even on an empty value: пустой ответ — тоже ответ), ``inferred``
   silences it only while the field holds a value (DRF-2397: вывод,
   который ничего не нашёл, — не знание), ``erased`` silences it by the
   DRF-1366 decision. Прочие пометки словаря (``behavioral``,
   ``conversational``, ``transactional``) не молчат вовсе: контракт
   внутреннего PATCH их принимает и пишет, но движок намеренно не считает
   их знанием — чем они на самом деле являются, решается отдельно, и до
   того решения вопрос по такому полю остаётся открытым.
6. **Organic or never** — wording responsibility on the caller; the
   engine just gates `should_ask`.
7. **Explainability** — verdict tuple includes a ``reason`` string
   so the surface can show "I'm asking because…" copy.
8. **Skip without penalty** — skipping a question never deletes
   data; only ``mark_asked`` and ``mark_skipped`` mutate state.

Event emission lives in ``personal_context_events`` so callers can
fire shown/answered/skipped/context-used events without touching the
analytics layer directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from users.models import UserPersonalContext
from users.personal_context_erasure import ERASED, declared_fields, default_for


logger = logging.getLogger("users.personalization")


# Tunables — bump as we learn from real usage.
COOLDOWN_HOURS = 24
DOUBLE_SKIP_PAUSE_DAYS = 30
SKIP_THRESHOLD_COUNT = 2

ALLOWED_REASON_CODES = frozenset({
    "ok",
    "already_have_data",
    "cooldown_24h",
    "double_skip_pause",
    "first_interaction",
    "context_missing",
})


@dataclass(frozen=True)
class Verdict:
    """Engine output. Caller renders or skips based on ``allowed``."""

    allowed: bool
    reason: str        # one of ALLOWED_REASON_CODES
    field: str

    def explanation(self) -> str:
        """Human-readable copy hint for surfaces that show "почему"."""
        return _REASON_COPY.get(self.reason, "")


_REASON_COPY = {
    "ok": "",
    "already_have_data": "У меня уже есть это.",
    "cooldown_24h": "Недавно спрашивала, повторно не буду.",
    "double_skip_pause": "Ты пропустил это 2 раза — возьму паузу.",
    "first_interaction": "Сначала закончим знакомство.",
    "context_missing": "Профиль ещё не создан — спрошу позже.",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _field_holds_value(ctx, field: str) -> bool:
    """Несёт ли поле что-нибудь о человеке — тем же мерилом, что у стирания.

    Сравнение с умолчанием модели (``default_for``), а не «пусто ли»: у разных
    полей пустота выглядит по-разному (``""``, ``[]``, ``False``, ``None``), и
    своя проверка разошлась бы с той, по которой стирание считает строку
    надгробием.
    """
    if field not in declared_fields():
        # Проверка закрывает два разных случая. Имя, которого у строки нет
        # вовсе, без неё падает на ``getattr`` — ``AttributeError`` (не на
        # ``default_for``: левый операнд считается первым). Имя, которое у
        # строки есть, но не входит в поля о человеке (``created_at``,
        # ``data_sources``), не падает нигде — и тем опаснее: знание по нему
        # заявлять не на чем. В обоих случаях правило 5 молчит о знании и
        # пропускает ход дальше.
        return False
    return getattr(ctx, field) != default_for(field)


def should_ask_question(user, field: str) -> Verdict:
    """Return a verdict on whether to ask the user about ``field``.

    Eight rules consulted in cheap-to-expensive order so common skips
    short-circuit fast.
    """
    # Rule 2 — onboarding gate (cheapest).
    if not getattr(user, "onboarding_completed", False):
        return Verdict(False, "first_interaction", field)

    ctx = UserPersonalContext.objects.filter(user=user).first()
    if ctx is None:
        # No context row yet but onboarding done — let the surface ask.
        # The PATCH endpoint lazy-creates the row on the first answer.
        return Verdict(True, "ok", field)

    # Rule 5 — already have data, or the subject erased it on purpose.
    #
    # DRF-2397: пометка `inferred` без значения — не знание, а ровно
    # обратное. `_infer_favorite_masters` ставит её БЕЗУСЛОВНО, тем, что
    # вернул запрос, и порога при этом не проверяет: у человека, у которого ни
    # с одним мастером нет трёх завершённых записей, это пустой список. Первый
    # же ночной проход закрывал «Есть любимый мастер, к кому вернуться?»
    # навсегда — вывод не нашёл ничего, а человека не спросили ни разу.
    #
    # Цена этой правки, названная честно: у `_infer_busy_days` пустой список
    # ПОСЛЕ порога истории — это вывод «избегать нечего», а не «не нашли», и
    # такому человеку вопрос про занятые дни снова откроется. Читатель этих
    # двух случаев не различает: их различает только писатель, который пометку
    # ставит. Сузить писателя (не штамповать вывод, ничего не нашедший) —
    # отдельная правка; здесь выбран читатель, потому что модуль вывода обещает
    # новые проходы, и каждый из них иначе пришлось бы проверять заново.
    #
    # Для `explicit` пустота — ОТВЕТ, а не незнание, и молчать по ней верно:
    # приложение ставит эту пометку всякому полю из тела PATCH, каким бы оно
    # ни было (`personal_context_views`), так что снятая галочка «дни, которые
    # лучше избегать» — это сказанное «нет таких». Бот (ai-bot-platform,
    # `orchestrator/memory/ayla_bridge.py`) пишет пустое значение так же,
    # отменяя прежний ответ: «снова ем мясо» — «диеты нет».
    #
    # Третий случай пустого `explicit` — просьба забыть одно поле
    # (`clear_declared_fields` там же): её честная пометка `erased`, но
    # внутренний PATCH такого значения не принимает. Молчание верно и здесь,
    # только по другой причине — по решению DRF-1366, а не потому что пустой
    # ответ это ответ. Выразить просьбу на контракте — задача владельца.
    #
    # ``erased`` — пустота тоже не незнание, а просьба человека (DRF-1366).
    sources = ctx.data_sources or {}
    source = sources.get(field)
    if source in {ERASED, "explicit"}:
        return Verdict(False, "already_have_data", field)
    if source == "inferred" and _field_holds_value(ctx, field):
        return Verdict(False, "already_have_data", field)

    # Rule 3 — 24h cooldown.
    last_asked = _parse_iso((ctx.last_asked_at or {}).get(field))
    if last_asked is not None:
        if _now() - last_asked < timedelta(hours=COOLDOWN_HOURS):
            return Verdict(False, "cooldown_24h", field)

    # Rule 4 — double-skip pause.
    skipped = (ctx.skipped_questions or {}).get(field) or {}
    if int(skipped.get("count") or 0) >= SKIP_THRESHOLD_COUNT:
        last_skip = _parse_iso(skipped.get("last_at"))
        if last_skip is None or (
            _now() - last_skip < timedelta(days=DOUBLE_SKIP_PAUSE_DAYS)
        ):
            return Verdict(False, "double_skip_pause", field)

    return Verdict(True, "ok", field)


def mark_asked(user, field: str) -> None:
    """Stamp last_asked_at for the field. Idempotent (always overwrites
    with current timestamp). Lazy-creates the context row.
    """
    ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
    last = dict(ctx.last_asked_at or {})
    last[field] = _now().isoformat()
    ctx.last_asked_at = last
    ctx.save(update_fields=["last_asked_at", "updated_at"])
    logger.debug("personalization.asked user=%s field=%s", user.pk, field)


def mark_skipped(user, field: str) -> int:
    """Increment skipped_questions counter for ``field`` and return the
    new count. The HTTP /skip/ endpoint already does this — exposing it
    here lets non-HTTP callers (AI Concierge in-process) record skips
    without going through DRF.
    """
    ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
    skipped = dict(ctx.skipped_questions or {})
    entry = dict(skipped.get(field) or {})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_at"] = _now().isoformat()
    skipped[field] = entry
    ctx.skipped_questions = skipped
    ctx.save(update_fields=["skipped_questions", "updated_at"])
    logger.debug(
        "personalization.skipped user=%s field=%s count=%d",
        user.pk, field, entry["count"],
    )
    return entry["count"]
