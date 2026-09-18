"""Дневник по дням за период — F10 (DRF-2099, решение владельца §48 п.7).

Одна строка на КАЖДЫЙ день периода — пустые дни присутствуют явно
(``has_entries: false``, ``kcal: null``): экран показывает «N из 7» как факт,
без «ты пропустил», стриков и напоминаний. Ретеншн-механики здесь нет и не
будет — это решение, а не недоделка.

**Сутки — по поясу человека.** :func:`person_timezone` — единственное
определение суток дневника: ``NutritionProfile.timezone``, если задан и
валиден, иначе UTC. Тот же предикат, что у воды v3
(``water_entry_service._load_nutrition_context``); умолчание UTC, не MSK —
так уже считает вода и так стоит профиль по умолчанию, тихая подмена на
MSK сдвинула бы дни всем. Этим же предикатом живут окно суток сводки
``?date=`` (:mod:`nutrition.services.nutrition_summary_service`) и
``get_commitment_days`` — иначе «открыть день» с недельного экрана показал
бы не те записи.

Предел: сегодня ``NutritionProfile.timezone`` никто не заполняет (анкета
бота поле не шлёт), поэтому у людей пилота сутки остаются UTC; источник
пояса — открытый вопрос владельца от 15.09, не этого модуля.

Дни считаются в Python по локальной дате каждой записи, не ``TruncDate``
в SQL: на границе перехода летнего времени SQL-усечение по поясу даёт
дубли и дыры, а список дат обязан быть ровно ``span + 1`` подряд.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone as dt_tz, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from nutrition.models import FoodLog, NutritionProfile

#: Верхняя граница периода — четыре недели (лист: ≤ 28 дней).
MAX_SPAN_DAYS = 28
DEFAULT_SPAN_DAYS = 7


def person_timezone(user_id: Any) -> tzinfo:
    """Пояс человека для суток дневника; UTC — когда пояса нет или он битый."""
    tz_name = (
        NutritionProfile.objects.filter(user_id=user_id)
        .values_list("timezone", flat=True)
        .first()
    )
    if not tz_name or tz_name == "UTC":
        return dt_tz.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — битое имя = нет пояса, не 500
        return dt_tz.utc


def timezone_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or "UTC"


def day_window(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """[start, end) локальных суток в aware-датах — одно определение на всех."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


@dataclass(frozen=True)
class DiaryDay:
    date: date
    meals_count: int
    kcal: float | None
    has_entries: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "meals_count": self.meals_count,
            "kcal": self.kcal,
            "has_entries": self.has_entries,
        }


class SpanTooLong(ValueError):
    pass


class BadPeriod(ValueError):
    pass


def default_period(tz: tzinfo, today: date | None = None) -> tuple[date, date]:
    """Семь дней до сегодня — по поясу человека, не по UTC."""
    end = today or datetime.now(tz).date()
    return end - timedelta(days=DEFAULT_SPAN_DAYS - 1), end


def diary_days(user, *, date_from: date, date_to: date, tz: tzinfo | None = None) -> list[DiaryDay]:
    if date_from > date_to:
        raise BadPeriod("from must not be after to")
    span = (date_to - date_from).days + 1
    if span > MAX_SPAN_DAYS:
        raise SpanTooLong(f"period must be at most {MAX_SPAN_DAYS} days")
    tz = tz or person_timezone(user.pk)
    start, _ = day_window(date_from, tz)
    _, end = day_window(date_to, tz)

    counts: dict[date, int] = defaultdict(int)
    kcal: dict[date, float] = defaultdict(float)
    for logged_at, calories in (
        FoodLog.objects.filter(user=user, logged_at__gte=start, logged_at__lt=end)
        .values_list("logged_at", "calories")
    ):
        local_day = logged_at.astimezone(tz).date()
        counts[local_day] += 1
        kcal[local_day] += float(calories or 0.0)

    rows: list[DiaryDay] = []
    for i in range(span):
        day = date_from + timedelta(days=i)
        n = counts.get(day, 0)
        rows.append(
            DiaryDay(
                date=day,
                meals_count=n,
                kcal=round(kcal[day], 1) if n else None,
                has_entries=n > 0,
            )
        )
    return rows
