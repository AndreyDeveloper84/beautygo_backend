"""Избранные блюда — сохранить, показать, скрыть (DRF-2092, дневник F12).

Один писатель для трёх ручек ``internal/saved-meals/``. Всё под субъектом:
каждая операция получает ``user`` и не заглядывает в чужие строки — чужая
запись дневника или чужое избранное для неё «не найдено», а не «чужое»:
по коду ответа нельзя перебирать чужие id.

Снимок, а не ссылка: ``portion_g`` и калории/БЖУ фиксируются на момент
сохранения. Из записи дневника порция считается как
``portion_multiplier × 100 г`` — ``FoodLog`` хранит множитель базовых 100 г,
а человек видит граммы.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from nutrition.models import FoodLog, SavedMeal

#: База ``FoodLog.portion_multiplier`` — тот же смысл, что у ``text_entry``
#: бота (``BASELINE_G``): множитель 1.0 = 100 г.
BASELINE_G = 100.0


class SavedMealError(Exception):
    """Base for saved-meal refusals."""


class SavedMealNotFoundError(SavedMealError):
    """Нет такой живой строки у этого человека — чужая или скрытая."""


class SourceFoodLogNotFoundError(SavedMealError):
    """Нет такой записи дневника у этого человека."""


@dataclass(frozen=True)
class SaveOutcome:
    meal: SavedMeal
    created: bool


class SavedMealService:
    def list_for(self, user) -> list[SavedMeal]:
        return list(
            SavedMeal.objects.filter(user=user, deleted_at__isnull=True)
            .order_by("-created_at", "-id")
        )

    def save(
        self,
        user,
        *,
        dish_name: str,
        portion_g: float,
        calories: float = 0.0,
        protein_g: float | None = None,
        fat_g: float | None = None,
        carbs_g: float | None = None,
        source_food_log: FoodLog | None = None,
    ) -> SaveOutcome:
        """Повтор того же блюда с той же порцией возвращает существующую строку."""
        dish_name = dish_name.strip()
        with transaction.atomic():
            existing = (
                SavedMeal.objects.select_for_update()
                .filter(user=user, dish_name=dish_name, portion_g=portion_g, deleted_at__isnull=True)
                .first()
            )
            if existing is not None:
                return SaveOutcome(meal=existing, created=False)
            meal = SavedMeal.objects.create(
                user=user,
                dish_name=dish_name,
                portion_g=portion_g,
                calories=calories,
                protein_g=protein_g,
                fat_g=fat_g,
                carbs_g=carbs_g,
                source_food_log=source_food_log,
            )
        return SaveOutcome(meal=meal, created=True)

    def save_from_food_log(self, user, food_log_id: UUID) -> SaveOutcome:
        """«Сохранить в избранное» из записи: снимок — из записи субъекта."""
        log = FoodLog.objects.filter(user=user, pk=food_log_id).first()
        if log is None:
            raise SourceFoodLogNotFoundError(str(food_log_id))
        return self.save(
            user,
            dish_name=log.dish_name,
            portion_g=float(log.portion_multiplier) * BASELINE_G,
            calories=log.calories,
            protein_g=log.protein_g,
            fat_g=log.fat_g,
            carbs_g=log.carbs_g,
            source_food_log=log,
        )

    def soft_delete(self, user, meal_id: UUID) -> SavedMeal:
        """Скрыть из списка. Скрытая или чужая строка — «не найдено»."""
        with transaction.atomic():
            meal = (
                SavedMeal.objects.select_for_update()
                .filter(user=user, pk=meal_id, deleted_at__isnull=True)
                .first()
            )
            if meal is None:
                raise SavedMealNotFoundError(str(meal_id))
            meal.deleted_at = timezone.now()
            meal.save(update_fields=["deleted_at"])
        return meal
