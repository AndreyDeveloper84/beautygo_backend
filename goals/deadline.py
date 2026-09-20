"""Срок цели — разбор и границы (DRF-2173, H01-2).

Решение владельца 20.09 («цена и срок нужны»): у цели МОЖЕТ быть срок —
«До 1 ноября 2026» в карточке цели на главном экране. Срок необязателен и
по умолчанию его нет; напоминаний по нему нет, процентов «времени прошло»
нет, план по сроку не пересчитывается (п.4 листа: §49/§82 — план про
действия, не про календарь).

Что здесь
---------

* :func:`resolve_target_date` — из ответа на шаг анкеты ``deadline`` (ключ
  варианта ИЛИ свободный текст) получить дату или ``None`` («без срока»).
  Разбор **детерминированный**: таблица форм ниже, ни LLM, ни угадывания.
  Что не разобралось — :class:`DeadlineError` с причиной и словами для 400,
  не «ближайшая похожая дата».
* Границы: не раньше сегодня, не дальше двух лет (включительно). Прошлое —
  отказ, а не «сдвинем на год»: человек написал конкретное, и подменять
  его молча нельзя (§48 «оставь как в чате» распространяется и сюда).
  Исключение — форма БЕЗ года («1 марта»): год не назван, и ближайший
  будущий — единственное честное прочтение.
* :func:`apply_target_date` — записать срок на цель, не трогая ничего
  больше (текст цели дословен).

Умолчания, названные владельцу отступлениями (тело PR):

* «через месяц» = сегодня + 1 календарный месяц (конец месяца прижимается:
  31.01 → 28.02);
* «к лету» = 1 июня ближайшего будущего лета (летом — следующего года);
  «к осени/зиме/весне» — 1 сентября / 1 декабря / 1 марта по тому же правилу;
  «к новому году» — 1 января.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .models import ClientGoal

#: Ключ шага анкеты (goals/anketa.py) — необязательный, последний.
DEADLINE_STEP_KEY = "deadline"

#: Варианты шага. ``NO_DEADLINE`` — умолчание: «без срока».
IN_MONTH = "in_month"
BY_SUMMER = "by_summer"
NO_DEADLINE = "no_deadline"
OPTION_KEYS: tuple[str, ...] = (IN_MONTH, BY_SUMMER, NO_DEADLINE)

#: Потолок: не дальше двух лет от сегодня (включительно).
MAX_YEARS_AHEAD = 2

REASON_PAST = "past"
REASON_TOO_FAR = "too_far"
REASON_UNREADABLE = "unreadable"

_MESSAGES = {
    REASON_PAST: "Этот срок уже прошёл — назови дату не раньше сегодняшней.",
    REASON_TOO_FAR: "Слишком далеко — давай выберем срок в пределах двух лет.",
    REASON_UNREADABLE: "Не поняла срок. Напиши, например, «до 1 ноября» или «через месяц».",
}


class DeadlineError(ValueError):
    """Срок не принят; ``reason`` — машинный код, ``message`` — слова для человека."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message = _MESSAGES[reason]
        super().__init__(self.message)


_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
#: Первое число сезона — «к лету» и т.п.
_SEASONS = {"лет": (6, 1), "осен": (9, 1), "зим": (12, 1), "весн": (3, 1)}
_NO_DEADLINE_PHRASES = ("без срока", "не знаю", "без даты", "пока нет", "неважно", "не важно")

_RE_DMY_DOT = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?$")
_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_DAY_MONTH = re.compile(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?$")
_RE_IN = re.compile(r"^через\s+(?:(\d+)\s+)?(недел|месяц|полгода|год)\w*$")
_RE_SEASON = re.compile(r"^(лет|осен|зим|весн)\w*$")
_RE_NEW_YEAR = re.compile(r"^нов\w*\s+год\w*$")


def _month_from_word(word: str) -> int | None:
    for stem, num in _MONTHS.items():
        if word.startswith(stem):
            # «ма» ловит «мая»/«марта» — «март» стоит выше по списку? Нет: dict
            # обходится по вставке, «март» раньше «ма», поэтому «марта» → 3.
            return num
    return None


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def _next_occurrence(month: int, day: int, *, today: date) -> date:
    """Ближайшая будущая (не раньше сегодня) дата с таким месяцем и днём."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            raise DeadlineError(REASON_UNREADABLE) from None
        if candidate > today:
            return candidate
    # today's month/day — «не раньше сегодня» строго: тот же день через год.
    return date(today.year + 1, month, day)


def _strip_prefix(text: str) -> str:
    text = text.strip().casefold().replace("ё", "е")
    for prefix in ("до ", "к ", "до конца ", "примерно ", "где-то "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text


def parse_deadline_text(text: str, *, today: date) -> date | None:
    """Свободный текст → дата, ``None`` для «без срока»; иначе :class:`DeadlineError`."""
    raw = _strip_prefix(text)
    if not raw:
        raise DeadlineError(REASON_UNREADABLE)
    if any(phrase in raw for phrase in _NO_DEADLINE_PHRASES):
        return None

    if (m := _RE_ISO.match(raw)):
        y, mo, d = (int(x) for x in m.groups())
        return _exact(y, mo, d, today=today)
    if (m := _RE_DMY_DOT.match(raw)):
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if y:
            return _exact(int(y), int(mo), int(d), today=today)
        return _bounded(_next_occurrence(int(mo), int(d), today=today), today=today)
    if (m := _RE_DAY_MONTH.match(raw)):
        d, word, y = int(m.group(1)), m.group(2), m.group(3)
        month = _month_from_word(word)
        if month is None:
            raise DeadlineError(REASON_UNREADABLE)
        if y:
            return _exact(int(y), month, d, today=today)
        return _bounded(_next_occurrence(month, d, today=today), today=today)
    if (m := _RE_IN.match(raw)):
        n = int(m.group(1) or 1)
        unit = m.group(2)
        if unit == "недел":
            target = today + timedelta(weeks=n)
        elif unit == "месяц":
            target = _add_months(today, n)
        elif unit == "полгода":
            target = _add_months(today, 6 * n)
        else:
            target = _add_months(today, 12 * n)
        return _bounded(target, today=today)
    if (m := _RE_SEASON.match(raw)):
        month, day = _SEASONS[m.group(1)]
        return _bounded(_next_occurrence(month, day, today=today), today=today)
    if _RE_NEW_YEAR.match(raw):
        return _bounded(date(today.year + 1, 1, 1), today=today)
    if raw in ("сегодня", "завтра", "вчера"):
        # «сегодня»/«вчера» — не срок цели; «завтра» — формально валиден.
        if raw == "завтра":
            return today + timedelta(days=1)
        raise DeadlineError(REASON_PAST if raw == "вчера" else REASON_UNREADABLE)
    raise DeadlineError(REASON_UNREADABLE)


def _exact(year: int, month: int, day: int, *, today: date) -> date:
    try:
        target = date(year, month, day)
    except ValueError:
        raise DeadlineError(REASON_UNREADABLE) from None
    return _bounded(target, today=today)


def _bounded(target: date, *, today: date) -> date:
    if target < today:
        raise DeadlineError(REASON_PAST)
    ceiling = _add_months(today, 12 * MAX_YEARS_AHEAD)
    if target > ceiling:
        raise DeadlineError(REASON_TOO_FAR)
    return target


def resolve_target_date(option_key: str | None, text: str | None, *, today: date | None = None) -> date | None:
    """Ответ на шаг ``deadline`` → дата или ``None`` («без срока»).

    Вариант имеет приоритет над текстом: экран шлёт одно из двух.
    """
    today = today or date.today()
    if option_key in (NO_DEADLINE, "unknown"):
        # «без срока» (лист) и «Не знаю» (макет C03, роль escape) — одно и то
        # же: срока нет. Ключ escape — ``anketa.UNKNOWN_OPTION_KEY``.
        return None
    if option_key == IN_MONTH:
        return _add_months(today, 1)
    if option_key == BY_SUMMER:
        return _bounded(_next_occurrence(6, 1, today=today), today=today)
    if option_key:
        raise DeadlineError(REASON_UNREADABLE)
    if text is None or not text.strip():
        return None
    return parse_deadline_text(text, today=today)


def apply_target_date(goal: ClientGoal, target_date: date | None) -> None:
    """Записать срок на цель — и только его (текст цели дословен, §48)."""
    goal.target_date = target_date
    goal.save(update_fields=["target_date", "updated_at"])


def target_date_passed(goal: ClientGoal, *, today: date | None = None) -> bool:
    """Факт «срок прошёл» — считает сервер, не экран."""
    if goal.target_date is None:
        return False
    return goal.target_date < (today or date.today())


@dataclass(frozen=True)
class DeadlineOption:
    key: str
    label: str


#: Подписи вариантов шага — слов «дата»/«время» здесь нет намеренно:
#: сторож границы C03 (DRF-1751) держит анкету без тем расписания.
OPTIONS: tuple[DeadlineOption, ...] = (
    DeadlineOption(IN_MONTH, "через месяц"),
    DeadlineOption(BY_SUMMER, "к лету"),
    DeadlineOption(NO_DEADLINE, "без срока"),
)
