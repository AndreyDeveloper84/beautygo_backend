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

import logging
from dataclasses import dataclass
from uuid import UUID

from django.db.models.functions import Lower

from services.goal_resolution import expand_categories_with_descendants
from services.models import GoalOption, GoalOptionCategory, ServiceCategory

from .models import ClientGoal

logger = logging.getLogger(__name__)


def _categories_for_option(option: GoalOption) -> list[UUID]:
    """Категории цели, раскрытые вниз до подкатегорий (DRF-1308).

    Владелец курирует связи на КОРНЕВЫХ категориях, а услуги висят на
    листьях, поэтому без раскрытия фильтр по ``SalonService.category_id``
    не находит ни одной услуги — замерено на контуре 23.08: 19 связей,
    все на корни, 0 совпадений напрямую.

    Порядок сохраняется: сначала корень (по ``sort_order`` связи), сразу
    за ним его подкатегории.
    """
    return expand_categories_with_descendants([cid for cid, _ in _bound_links(option)])


def _bound_links(option: GoalOption) -> list[tuple[UUID, int]]:
    """Связи цели с категориями по ``sort_order`` — ОДИН запрос на оба читателя.

    И фильтр совпадения (:func:`_categories_for_option`), и положение категорий
    (:func:`category_positions_for_option`) берут связи отсюда: условие,
    добавленное в одну копию запроса, не может молча разойтись с другой.
    """
    return list(
        GoalOptionCategory.objects.filter(goal_option=option)
        .order_by("sort_order")
        .values_list("category_id", "sort_order")
    )


@dataclass(frozen=True)
class GoalCategoryPosition:
    """Где категория стоит в цели — для глубины совпадения (DRF-2789, R0, вариант А владельца).

    ``primary`` — категория относится к ОСНОВНОЙ ветке цели: сама связана
    основной связью или лежит под основным корнем. Основная связь — та, у
    которой ``sort_order`` наименьший И единственный; если наименьший делят
    несколько связей или порядок не задан вовсе (все по умолчанию 0) — основной
    ветки нет: неоднозначный приоритет не становится молча «основным».
    ``direct`` — категория сама связана с целью; ``False`` — досталась
    раскрытием связанного корня.

    Решение владельца 06.10 (вариант А): потомок НАСЛЕДУЕТ класс ветки, и само
    раскрытие не опускает его ниже прямой связи побочной ветки. Раскрытие
    различает только внутри класса. Положение в дереве — это курируемая
    принадлежность к цели, а не утверждение об эффективности процедуры:
    об эффективности говорит только подтверждённая ``CapabilityGoalLink``.
    """

    direct: bool
    primary: bool

    @property
    def rank(self) -> int:
        """Порядок внутри уровня «категория цели»: класс ветки важнее раскрытия."""
        return (2 if self.primary else 0) + (1 if self.direct else 0)


def category_positions_for_option(option: GoalOption) -> dict[UUID, GoalCategoryPosition]:
    """Категории цели в порядке :func:`_categories_for_option` — с их положением.

    Ключи и их порядок — ровно то, что отдаёт ``_categories_for_option``:
    одно раскрытие на оба вопроса («какие категории» и «где каждая стоит»).

    **Несколько путей к категории** (связана прямо И лежит под связанным
    корнем) дают ОДНО положение — лучшее по ``rank`` среди всех путей.
    Максимум не зависит от порядка обхода, а словарь не держит дублей.
    """
    bound = _bound_links(option)
    if not bound:
        return {}
    orders = [order for _, order in bound]
    lowest = min(orders)
    has_primary = orders.count(lowest) == 1
    if not has_primary:
        logger.warning(
            "goals.positions.ambiguous_primary goal=%s lowest_sort_order=%s links=%d — "
            "основной ветки нет, все связи считаются побочными (fail-closed)",
            option.key, lowest, orders.count(lowest),
        )
    primary_of_bound = {cid: has_primary and order == lowest for cid, order in bound}
    expanded = expand_categories_with_descendants([cid for cid, _ in bound])
    parent_of = dict(
        ServiceCategory.objects.filter(pk__in=expanded).values_list("id", "parent_id")
    )
    positions: dict[UUID, GoalCategoryPosition] = {}
    for cid in expanded:
        paths = []
        if cid in primary_of_bound:
            paths.append(GoalCategoryPosition(direct=True, primary=primary_of_bound[cid]))
        parent = parent_of.get(cid)
        if parent in primary_of_bound:
            paths.append(GoalCategoryPosition(direct=False, primary=primary_of_bound[parent]))
        # Пути нет только у категории, которой раскрытие не должно было дать:
        # побочная по умолчанию, без «основного» из воздуха.
        positions[cid] = max(paths, key=lambda pos: pos.rank) if paths else GoalCategoryPosition(
            direct=False, primary=False,
        )
    return positions


def resolve_goal_category_ids(client) -> list[UUID] | None:
    """Активная цель клиента → упорядоченный набор category_id.

    ``None`` — разрешить нельзя (нет цели, нет маппинга, свободный текст
    без точного совпадения): вызывающая сторона обязана уточнить,
    а не угадывать.
    """
    goal = (
        ClientGoal.objects.filter(client=client, state=ClientGoal.State.ACTIVE)
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
