"""Поставщики фактов для Plan Adherence (DRF-1334, контракт §3).

Контракт поставщика: ответить «сколько фактов типа T у человека в интервале
[start, end)» — **число, без слова «выполнено»**. Решение о выполнении плана
принимает домен Personal Plan (`wellness/adherence.py`), не поставщик.

Направление зависимости одно (В-2): wellness читает журналы Nutrition
(`WaterEntry`, `FoodLog`); Nutrition про wellness не знает. Профиль питания
здесь не читается никогда — это журналы фактов, не нормы.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from nutrition.models import FoodLog, WaterEntry

from .models import PlanAction


def _count_water(user_id: UUID, start: datetime, end: datetime) -> int:
    """Записи воды в интервале; мягко удалённые не считаются фактом."""
    return WaterEntry.objects.filter(
        user_id=user_id,
        ts__gte=start,
        ts__lt=end,
        deleted_at__isnull=True,
    ).count()


def _count_food(user_id: UUID, start: datetime, end: datetime) -> int:
    return FoodLog.objects.filter(
        user_id=user_id,
        logged_at__gte=start,
        logged_at__lt=end,
    ).count()


#: Брони, которые считаются фактом «записался»: подтверждённые и уже
#: состоявшиеся. Отменённые и неявки — не факт действия; ожидающие
#: подтверждения/оплаты — ещё не бронь. Статусы из `appointments.models`,
#: не переписаны здесь как строки.
def _booking_statuses() -> tuple[str, ...]:
    from appointments.models import Appointment

    return (Appointment.Status.CONFIRMED, Appointment.Status.COMPLETED)


def _count_bookings(
    user_id: UUID, start: datetime, end: datetime, *, goal_key: str | None = None,
) -> int:
    """DRF-2101 — брони человека, СДЕЛАННЫЕ в интервале (``created_at``):
    обязательство «записаться» — про действие записи, не про дату визита.

    ``goal_key`` — категория услуги ∈ категориям курируемой цели
    (``GoalOption → GoalOptionCategory``); без ключа или без категорий у
    цели — по времени. Предел (DRF-1931): бронь не несёт ``goal_id``, и
    «под цель» здесь — совпадение категории, не намерение человека.
    """
    from appointments.models import Appointment
    from django.db.models import Q
    from services.models import GoalOptionCategory

    qs = Appointment.objects.filter(
        client_id=user_id,
        status__in=_booking_statuses(),
        created_at__gte=start,
        created_at__lt=end,
    )
    if goal_key:
        category_ids = list(
            GoalOptionCategory.objects.filter(goal_option__key=goal_key)
            .values_list("category_id", flat=True)
        )
        if category_ids:
            qs = qs.filter(
                Q(service__category_id__in=category_ids)
                | Q(salon_service__category_id__in=category_ids)
            )
    return qs.count()


_PROVIDERS = {
    PlanAction.ActionType.LOG_WATER: _count_water,
    PlanAction.ActionType.LOG_FOOD: _count_food,
}


def count_facts(
    action_type: str,
    user_id: UUID,
    start: datetime,
    end: datetime,
    *,
    goal_key: str | None = None,
) -> int:
    """Число фактов типа `action_type` в [start, end).

    Неизвестный ключ — программная ошибка (ключи курируются, контракт §2),
    поэтому ValueError, а не тихий ноль. ``goal_key`` читает только
    ``book_service`` (DRF-2101); журналам питания цель безразлична.
    """
    if action_type == PlanAction.ActionType.BOOK_SERVICE:
        return _count_bookings(user_id, start, end, goal_key=goal_key)
    try:
        provider = _PROVIDERS[action_type]
    except KeyError:
        raise ValueError(
            f"no fact provider for action_type={action_type!r}"
        ) from None
    return provider(user_id, start, end)


def count_fact_days(
    action_type: str,
    user_id: UUID,
    start: datetime,
    end: datetime,
) -> int:
    """Число ДНЕЙ в [start, end) хотя бы с одним фактом журнала питания
    (DRF-2101: недельное обязательство «дневник N дней» считает дни, а не
    записи — семь записей в один день это один день). Только для
    ``log_food`` / ``log_water``; для броней дни не считаются."""
    from django.db.models.functions import TruncDate

    if action_type == PlanAction.ActionType.LOG_FOOD:
        qs = FoodLog.objects.filter(user_id=user_id, logged_at__gte=start, logged_at__lt=end)
        field = "logged_at"
    elif action_type == PlanAction.ActionType.LOG_WATER:
        qs = WaterEntry.objects.filter(
            user_id=user_id, ts__gte=start, ts__lt=end, deleted_at__isnull=True,
        )
        field = "ts"
    else:
        raise ValueError(f"no day-count provider for action_type={action_type!r}")
    return qs.annotate(day=TruncDate(field)).values("day").distinct().count()
