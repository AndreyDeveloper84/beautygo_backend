"""Факты дневника ДЛЯ плана, которым нужен ориентир (DRF-2124, План-B).

Поставщики фактов wellness (``wellness/fact_providers.py``) профиль питания
не читают никогда — это журналы, не нормы. Факт «день в ориентире» без
ориентира не существует, поэтому он живёт здесь, в ``nutrition``: направление
зависимости прежнее (В-2: wellness читает nutrition), а граница В-1 держится —
``plan_lite*.py`` импортирует функцию, не ``NutritionProfile``.

Правило одно и арифметическое: день считается «в ориентире», если ориентир по
калориям ДЕЙСТВУЕТ (``calories_confirmed`` — по происхождению, F1(б); §5.1:
предложение — не ориентир) и сумма калорий записей этого дня ≤ ``daily_kcal``.
Без действующего ориентира ответ — ``None``, не 0 (§103: нет входа — нет
числа). Никаких процентов и оценок (§85, В-5).

Предел, названный, а не спрятанный: «≤ ориентира» — это «не превысил», а не
«дописал день целиком». День с одной записью на 200 ккал попадёт в счёт;
кто читает число, не должен подавать его как достижение.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate

from nutrition.models import FoodLog, NutritionProfile
from nutrition.services.targets_state import calories_confirmed


def count_days_within_calorie_target(user_id: UUID, start: datetime, end: datetime) -> int | None:
    """Число дней в [start, end) с записями еды, чья дневная сумма ≤ действующему
    ориентиру по калориям; ``None`` — ориентир не действует.

    Дни без записей не считаются: «0 ≤ ориентира» — не факт о еде. Группировка
    по календарному дню в текущей зоне (``TruncDate``) — та же, что у счёта дней
    с записью в wellness (``count_fact_days``), чтобы числа были сравнимы.
    """
    profile = NutritionProfile.objects.filter(user_id=user_id).first()
    # ``calories_confirmed(None)`` — False, так что после проверки профиль есть.
    if not calories_confirmed(profile) or not profile.daily_kcal:
        return None
    target = float(profile.daily_kcal)
    per_day = (
        FoodLog.objects.filter(user_id=user_id, logged_at__gte=start, logged_at__lt=end)
        .annotate(day=TruncDate("logged_at"))
        .values("day")
        .annotate(
            total=Sum("calories"),
            unscored=Count("pk", filter=Q(calories__isnull=True)),
        )
    )
    # DRF-2371 — день, в котором часть съеденного не посчитана, не «уложился
    # в ориентир»: он неизвестен. Прежний комментарий утверждал, что ``Sum``
    # не бывает ``None``, — с тех пор как макросы стали nullable, это неверно
    # дважды: сумма может быть ``None``, а частичная сумма «300 из 2000»
    # зачла бы день молча.
    return sum(
        1
        for row in per_day
        if row["unscored"] == 0 and row["total"] is not None and row["total"] <= target
    )
