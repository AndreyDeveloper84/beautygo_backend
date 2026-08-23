"""DRF-1317 — цель снимается с корня и переносится на точные подкатегории.

Дефект: связь «цель → категория» курировалась на КОРНЕ «Массаж тела», а
резолвер DRF-1308 отдаёт цели корня всякому листу без собственной связи.
В итоге все 25 массажей ветки — включая массаж головы, массаж стоп и
детский — отвечали разом на «Расслабиться», «Подтянуть фигуру» и
«Восстановить силы».

Чинится данными: связь переезжает с корня на четыре подкатегории, где
она честна. Единственное, чего сид не умел, — снять связь; отсюда
``--prune``.

Здесь проверяется механика, от которой это зависит:

- лист со своей связью перебивает корень (регрессионный якорь: если
  порядок разрешения в ``goal_resolution`` когда-нибудь поменяют на
  объединение, тест покажет это здесь, а не на живом ходу);
- без ``--prune`` связь корня остаётся, и лист без своей связи
  продолжает её наследовать — то есть флаг действительно необходим,
  а не декоративен;
- ``--prune`` снимает только связи ОБЪЯВЛЕННЫХ в файле целей: строка,
  добавленная владельцем через админку для цели, которой в файле нет,
  переживает прогон;
- ``--dry-run --prune`` ничего не удаляет.
"""
from __future__ import annotations

import json

import pytest
from django.core.management import call_command

from services.goal_resolution import build_category_goal_index
from services.models import GoalOption, GoalOptionCategory, ServiceCategory

ROOT = "Массаж тела"
LEAF_RELAX = "Базовый ручной массаж"
LEAF_HEAL = "Оздоровительные и восстановительные массажи"
LEAF_KIDS = "Детский массаж"

FILE_ROWS = [
    {
        "key": "relax",
        "label": "Расслабиться и снять стресс",
        "sort_order": 10,
        "categories": [LEAF_RELAX],
    },
    {
        "key": "recharge",
        "label": "Восстановить силы",
        "sort_order": 70,
        "categories": [LEAF_HEAL],
    },
]


@pytest.fixture
def massage_tree(db):
    """Форма пилота: корень со связями, три листа без своих связей."""
    root = ServiceCategory.objects.create(name=ROOT)
    leaves = {
        name: ServiceCategory.objects.create(name=name, parent=root)
        for name in (LEAF_RELAX, LEAF_HEAL, LEAF_KIDS)
    }
    options = {
        row["key"]: GoalOption.objects.create(
            key=row["key"], label=row["label"], sort_order=row["sort_order"],
        )
        for row in FILE_ROWS
    }
    for option in options.values():
        GoalOptionCategory.objects.create(goal_option=option, category=root)
    return {"root": root, "leaves": leaves, "options": options}


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "goal_options.json"
    path.write_text(json.dumps(FILE_ROWS, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _keys(index, category) -> list[str]:
    return [goal["key"] for goal in index.goals_for_category(category.id)]


@pytest.mark.django_db
def test_root_links_leak_all_goals_onto_every_leaf(massage_tree):
    """Состояние ДО правки — три цели на каждом листе, включая детский."""
    index = build_category_goal_index()
    for leaf in massage_tree["leaves"].values():
        assert _keys(index, leaf) == ["relax", "recharge"]


@pytest.mark.django_db
def test_leaf_link_overrides_root_but_root_still_leaks_without_prune(
    massage_tree, seed_file,
):
    """Сид без ``--prune``: листы со связью честны, лист без связи — нет."""
    call_command("seed_goal_options", "--file", seed_file)

    index = build_category_goal_index()
    leaves = massage_tree["leaves"]
    assert _keys(index, leaves[LEAF_RELAX]) == ["relax"]
    assert _keys(index, leaves[LEAF_HEAL]) == ["recharge"]
    # Ровно та строка, ради которой флаг и понадобился.
    assert _keys(index, leaves[LEAF_KIDS]) == ["relax", "recharge"]


@pytest.mark.django_db
def test_prune_removes_root_links_and_kids_leaf_becomes_honestly_empty(
    massage_tree, seed_file,
):
    call_command("seed_goal_options", "--file", seed_file, "--prune")

    root = massage_tree["root"]
    assert not GoalOptionCategory.objects.filter(category=root).exists()

    index = build_category_goal_index()
    leaves = massage_tree["leaves"]
    assert _keys(index, leaves[LEAF_RELAX]) == ["relax"]
    assert _keys(index, leaves[LEAF_HEAL]) == ["recharge"]
    assert _keys(index, leaves[LEAF_KIDS]) == []


@pytest.mark.django_db
def test_prune_spares_hand_curated_link_of_an_undeclared_goal(
    massage_tree, seed_file,
):
    """Цель, которой нет в файле, сид не судит — её связь остаётся.

    На контуре 23.08 одна из 19 связей поставлена владельцем через
    админку; молчаливое удаление стёрло бы его решение.
    """
    hand_made = GoalOption.objects.create(key="body_shape", label="Подтянуть фигуру")
    link = GoalOptionCategory.objects.create(
        goal_option=hand_made, category=massage_tree["root"],
    )

    call_command("seed_goal_options", "--file", seed_file, "--prune")

    assert GoalOptionCategory.objects.filter(pk=link.pk).exists()


@pytest.mark.django_db
def test_dry_run_with_prune_deletes_nothing(massage_tree, seed_file):
    before = set(GoalOptionCategory.objects.values_list("pk", flat=True))

    call_command("seed_goal_options", "--file", seed_file, "--dry-run", "--prune")

    assert set(GoalOptionCategory.objects.values_list("pk", flat=True)) == before
