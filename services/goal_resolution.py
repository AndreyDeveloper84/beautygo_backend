"""Разрешение «услуга ↔ цель» по дереву категорий (DRF-1308).

Почему модуль вообще нужен
--------------------------
`GoalOptionCategory` связывает цель с категорией. На пилотном контуре
**все 19 связей ведут на корни, а все 94 услуги висят на листьях**, поэтому
прямое сравнение `SalonService.category_id == GoalOptionCategory.category_id`
даёт **ноль** совпадений. Дерево гарантированно двухуровневое
(`ServiceCategory.clean()` запрещает третий уровень), так что обход
тривиален и делается одним запросом в обе стороны:

* **цель → категории** (`expand_categories_with_descendants`) — корень
  раскрывается на себя и своих детей, чтобы фильтр по `category_id` находил
  услуги на листьях. Это направление нужно резолверу `goals.resolution`.
* **услуга → цели** (`CategoryGoalIndex`) — от категории услуги вверх к
  ближайшему предку со связью. Это направление нужно каталожному зеркалу:
  бот хранит UUID категории в `raw` и своей таблицы категорий не имеет,
  поэтому дерево разрешается на стороне Ayla, а в ответе едут уже готовые
  цели (ADR-0009: зеркало — не источник).

Решение владельца (DRF-1308, 23.08), п. 1: «если у service нет прямой
semantic binding, resolver поднимается к ближайшему родителю с binding».
Именно **fallback**, а не объединение: своя связь, если есть, побеждает.

Фолбэк на шаблон
----------------
У салонной услуги может не быть цели ни на своей категории, ни на её
родителе — потому что собственная категория салона это коммерческий
контейнер («Комплексные программы и пакеты»), а не предметная ветка.
Если услуга привязана к каноническому `ServiceTemplate`, категория шаблона
— более авторитетный семантический якорь, и цели берутся оттуда.

Порядок сознательно консервативный: шаблон подключается, **только** если
собственная цепочка не дала ничего. Замер на контуре 23.08: у 32 услуг
цели выводятся обоими путями и **расходятся в 0 случаях**, ещё у 3 —
только через шаблон. То есть на сегодняшних данных fallback и объединение
дают одно и то же, а fallback не может добавить ложную цель, если салон
ошибётся с категорией.

Границы
-------
Здесь нет ранжирования и нет угадывания. Цель у услуги либо выведена из
курируемой связи владельца, либо её нет (`[]`). LLM не является ranking
authority — AYLA-DEC-0045 / OD-9.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Iterable
from uuid import UUID

from .models import GoalOption, GoalOptionCategory, ServiceCategory

if TYPE_CHECKING:  # pragma: no cover
    from .models import SalonService


def expand_categories_with_descendants(
    category_ids: Iterable[UUID],
) -> list[UUID]:
    """`[корень, …]` → `[корень, его дети, …]` без дублей, порядок сохранён.

    Цели курируются на корнях, услуги живут на листьях; без раскрытия
    фильтр по `category_id` не находит ничего. Дерево не глубже двух
    уровней (`ServiceCategory.clean()`), поэтому одного запроса хватает.

    Неактивные подкатегории пропускаются: скрытая ветка каталога не должна
    возвращаться в выдачу через цель.
    """
    roots = list(dict.fromkeys(category_ids))
    if not roots:
        return []

    children_by_parent: dict[UUID, list[UUID]] = defaultdict(list)
    child_rows = (
        ServiceCategory.objects
        .filter(parent_id__in=roots, is_active=True)
        .order_by('sort_order', 'name')
        .values_list('id', 'parent_id')
    )
    for child_id, parent_id in child_rows:
        children_by_parent[parent_id].append(child_id)

    expanded: list[UUID] = []
    seen: set[UUID] = set()
    for root_id in roots:
        for candidate in (root_id, *children_by_parent.get(root_id, ())):
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


class CategoryGoalIndex:
    """Снимок «категория → цели», собранный за фиксированное число запросов.

    Строится один раз на запрос и кладётся в контекст сериализатора: иначе
    каждая из 94 строк каталога тянула бы связи целей отдельно (N+1).
    Данных мало по построению — цели и связи курирует владелец вручную
    (на контуре 23.08: 7 целей, 19 связей, 85 категорий).
    """

    __slots__ = ('_direct', '_parent_of')

    def __init__(
        self,
        direct: dict[UUID, list[dict[str, str]]],
        parent_of: dict[UUID, UUID | None],
    ) -> None:
        self._direct = direct
        self._parent_of = parent_of

    def goals_for_category(self, category_id: UUID | None) -> list[dict[str, str]]:
        """Цели категории: свои, иначе ближайшего предка со связью.

        Пункт 1 решения владельца. Дерево двухуровневое, поэтому «ближайший
        предок» — ровно один шаг, но цикл написан общим на случай, если
        ограничение глубины когда-нибудь снимут.
        """
        current = category_id
        seen: set[UUID] = set()
        while current is not None and current not in seen:
            seen.add(current)
            goals = self._direct.get(current)
            if goals:
                return goals
            current = self._parent_of.get(current)
        return []

    def goals_for_service(self, service: 'SalonService') -> list[dict[str, str]]:
        """Цели услуги: своя категория, иначе категория канонического шаблона.

        Собственная категория салона — коммерческий контейнер и может быть
        бесцелевой («Комплексные программы и пакеты»); категория шаблона —
        предметная ветка канона. Шаблон подключается только когда своя
        цепочка пуста, чтобы не приписать услуге цель, которой владелец
        для неё не заявлял.
        """
        goals = self.goals_for_category(service.category_id)
        if goals:
            return goals
        template = service.template
        if template is not None:
            return self.goals_for_category(template.category_id)
        return []


def build_category_goal_index() -> CategoryGoalIndex:
    """Собрать индекс «категория → цели» тремя запросами.

    Только активные цели: снятая с публикации цель не должна продолжать
    ездить в зеркало бота.
    """
    labels_by_option: dict[UUID, tuple[str, str, int]] = {
        option_id: (key, label, sort_order)
        for option_id, key, label, sort_order in GoalOption.objects
        .filter(is_active=True)
        .values_list('id', 'key', 'label', 'sort_order')
    }

    direct: dict[UUID, list[tuple[int, str, str]]] = defaultdict(list)
    link_rows = (
        GoalOptionCategory.objects
        .filter(goal_option_id__in=labels_by_option)
        .values_list('category_id', 'goal_option_id')
    )
    for category_id, option_id in link_rows:
        key, label, sort_order = labels_by_option[option_id]
        direct[category_id].append((sort_order, key, label))

    ordered_direct: dict[UUID, list[dict[str, str]]] = {
        category_id: [
            {'key': key, 'label': label}
            for _, key, label in sorted(entries)
        ]
        for category_id, entries in direct.items()
    }

    parent_of: dict[UUID, UUID | None] = dict(
        ServiceCategory.objects.values_list('id', 'parent_id')
    )
    return CategoryGoalIndex(ordered_direct, parent_of)
