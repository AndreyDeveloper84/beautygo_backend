"""Действует ли ориентир — один предикат на всех читателей (§5.1, §6).

Решение владельца 11.09.2026 §5.1: «результат становится привычкой/шагом
только после подтверждения пользователя». Значит у ориентира есть
состояние «посчитан, но не подтверждён» — ``ayla_proposed`` — и в нём
число показывается человеку как предложение, но НЕ участвует ни в
«осталось на сегодня», ни в оценках «мало/много», ни в паттернах и
инсайтах: для них предложение равно отсутствию (§6: неизвестные нормы —
``NOT_CONFIGURED``, а не ноль; и не «почти норма»).

Множество действующих источников перечислено ЯВНО и совпадает с тем,
что читает бот (``targets_are_configured`` ⇔ источник ∈ {``ayla_calculated``,
``user_entered``}). Всё, что не названо, — не действует: новое состояние
источника попадёт сюда только руками, а не по умолчанию.

Читатели значений (``daily_kcal``, ``daily_protein_g``, RDA) обязаны
спрашивать этот предикат, а не истинность числа: у ``ayla_proposed``
число есть, и по нему одному предложение неотличимо от подтверждённого.
"""
from __future__ import annotations

from nutrition.models import NutritionProfile

#: Источники, при которых ориентир ДЕЙСТВУЕТ. Расширять только решением
#: владельца и синхронно с ботом (``TARGETS_CONFIGURED_SOURCES``).
CONFIRMED_SOURCES: frozenset[str] = frozenset({
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
    NutritionProfile.TargetsSource.USER_ENTERED,
})


def targets_confirmed(profile: NutritionProfile | None) -> bool:
    """Действует ли ориентир профиля — по происхождению, не по числу."""
    if profile is None:
        return False
    return profile.targets_source in CONFIRMED_SOURCES
