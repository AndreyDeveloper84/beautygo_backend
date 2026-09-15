"""Дерево категорий канона — одно определение «направления» (DRF-1912, DRF-1799).

**Направление** шаблона — самый верхний предок его категории на любой
глубине. Это определение живёт здесь и только здесь: группа экрана 04 в
ответе выбора услуг (DRF-1912) и шаблоны направления на экране 03
(``?direction_id=``, DRF-1799) берут его из одной функции, а не из двух
похожих обходов.

**Какой корень — направление.** Тот же предикат, что у
``GET /internal/services/directions/``: ``parent IS NULL``, ``tenant IS NULL``,
``is_active``. Салонный корень (``tenant`` задан) — корень, но не направление.

Дерево грузится целиком одним запросом и проходится в Python, поэтому число
запросов не зависит ни от числа категорий, ни от глубины. Цикл в данных не
вешает проход: он останавливается на уже виденной вершине.

Оговорка: корни канона (22) ≠ 6 направлений экрана 02 — открытый вопрос
владельцу G7. До решения направление = корень канона; ответ G7 меняет данные
или этот модуль, а не экраны.
"""
from __future__ import annotations

from typing import Any

from .models import ServiceCategory

Node = dict[str, Any]


def category_nodes() -> dict[Any, Node]:
    """Все категории одним запросом: ``{id: {id, parent_id, tenant_id, name, sort_order, is_active}}``."""
    return {
        node["id"]: node
        for node in ServiceCategory.objects.values(
            "id", "parent_id", "tenant_id", "name", "sort_order", "is_active",
        )
    }


def category_roots(category_ids: set, nodes: dict[Any, Node] | None = None) -> dict:
    """Самый верхний предок каждой категории — ``{category_id: node | None}``."""
    if not category_ids:
        return {}
    if nodes is None:
        nodes = category_nodes()
    roots = {}
    for category_id in category_ids:
        current = category_id
        seen = {current}
        while current in nodes:
            parent = nodes[current]["parent_id"]
            if parent is None or parent not in nodes or parent in seen:
                break
            seen.add(parent)
            current = parent
        roots[category_id] = nodes.get(current)
    return roots


def is_direction(node: Node | None) -> bool:
    """Корень глобальной таксономии — тот же предикат, что у ``/directions/``."""
    return (
        node is not None
        and node["parent_id"] is None
        and node["tenant_id"] is None
        and bool(node["is_active"])
    )


def direction_category_ids(direction_id, nodes: dict[Any, Node] | None = None) -> set:
    """Все категории, чьё направление — ``direction_id``, включая сам корень."""
    if nodes is None:
        nodes = category_nodes()
    roots = category_roots(set(nodes), nodes)
    return {
        category_id
        for category_id, root in roots.items()
        if root is not None and root["id"] == direction_id
    }
