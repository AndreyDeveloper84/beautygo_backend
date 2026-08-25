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


_PROVIDERS = {
    PlanAction.ActionType.LOG_WATER: _count_water,
    PlanAction.ActionType.LOG_FOOD: _count_food,
}


def count_facts(
    action_type: str,
    user_id: UUID,
    start: datetime,
    end: datetime,
) -> int:
    """Число фактов типа `action_type` в [start, end).

    Неизвестный ключ — программная ошибка (ключи курируются, контракт §2),
    поэтому ValueError, а не тихий ноль.
    """
    try:
        provider = _PROVIDERS[action_type]
    except KeyError:
        raise ValueError(
            f"no fact provider for action_type={action_type!r}"
        ) from None
    return provider(user_id, start, end)
