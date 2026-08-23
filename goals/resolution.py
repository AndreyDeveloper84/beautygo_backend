"""Разрешение цели клиента в категории каталога.

Граница из отчёта по вопросу 1 (принята Ответом 2): знание
«цель → услуги» живёт в каталоге как данные (GoalOptionCategory),
движок рекомендаций о целях не знает — вызывающая сторона передаёт ему
уже разрешённые category_id.

OD-1 / Ответ 3: свободный текст при низкой уверенности НЕ отображается
насильно в ближайший чип. Поэтому для `goal_text` — только точное
совпадение по label (casefold); всё остальное — ``None`` = «уточнить».
Порог уверенности сознательно не вводится: калибровать его не на чем
(OD-2, корпуса нет), а при проекции переспрос — нормальный цикл,
а не тупик.

Подключение резолвера к вызывающим (RecommendationQuery и др.) — за
флагом ``GOAL_RESOLUTION_ENABLED`` и отдельным изменением; здесь только
чистая функция.

DRF-1308: цели курируются на корневых категориях, услуги висят на листьях,
поэтому результат раскрывается вниз до подкатегорий
(``services.goal_resolution.expand_categories_with_descendants``). Обратное
направление — «услуга → цели» для каталожного зеркала бота — живёт там же.
"""
from __future__ import annotations

from uuid import UUID

from django.db.models.functions import Lower

from services.goal_resolution import expand_categories_with_descendants
from services.models import GoalOption, GoalOptionCategory

from .models import ClientGoal


def _categories_for_option(option: GoalOption) -> list[UUID]:
    """Категории цели, раскрытые вниз до подкатегорий (DRF-1308).

    Владелец курирует связи на КОРНЕВЫХ категориях, а услуги висят на
    листьях, поэтому без раскрытия фильтр по ``SalonService.category_id``
    не находит ни одной услуги — замерено на контуре 23.08: 19 связей,
    все на корни, 0 совпадений напрямую.

    Порядок сохраняется: сначала корень (по ``sort_order`` связи), сразу
    за ним его подкатегории.
    """
    bound = list(
        GoalOptionCategory.objects.filter(goal_option=option)
        .order_by("sort_order")
        .values_list("category_id", flat=True)
    )
    return expand_categories_with_descendants(bound)


def resolve_goal_category_ids(client) -> list[UUID] | None:
    """Активная цель клиента → упорядоченный набор category_id.

    ``None`` — разрешить нельзя (нет цели, нет маппинга, свободный текст
    без точного совпадения): вызывающая сторона обязана уточнить,
    а не угадывать.
    """
    goal = (
        ClientGoal.objects.filter(client=client, is_active=True)
        .order_by("-selected_at")
        .first()
    )
    if goal is None:
        return None

    if goal.goal_key:
        option = GoalOption.objects.filter(key=goal.goal_key).first()
        if option is None:
            return None
        return _categories_for_option(option) or None

    if goal.goal_text:
        normalized = goal.goal_text.strip().casefold()
        option = (
            GoalOption.objects.filter(is_active=True)
            .annotate(label_lower=Lower("label"))
            .filter(label_lower=normalized)
            .first()
        )
        if option is None:
            return None
        return _categories_for_option(option) or None

    return None  # pragma: no cover — CheckConstraint не допускает
