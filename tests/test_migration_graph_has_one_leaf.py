"""У каждого приложения ровно один лист в графе миграций — DRF-1240 / падение пилота 11.09.

Что случилось. #349 добавил ``appointments/0018_state_drift_drf1128``, #340 —
``appointments/0018_drf1240_time_off_author``; обе зависят от ``0017``. Каждый
PR был MERGEABLE и зелёным: конфликт двух листьев не ловится ни слиянием, ни
тестами, а проявляется только на ``migrate``. Entrypoint web делает
``migrate`` при старте → «Conflicting migrations» → crashloop, 301 рестарт,
admin 502, пилот лёг в 14:58 UTC. Починено ``0019_merge`` (#370).

Почему сторож, а не аккуратность. Между генерацией миграции и слиянием PR
проходит время, и за это время в dev вливается чужая миграция того же
приложения. Автор второй не видит первую по построению — она ещё не на его
базе. Единственное место, где обе видны сразу, — граф на ``dev`` после
слияния, и это место обязан смотреть тот, кто там всегда: CI.

Проверка идёт по загрузчику миграций без обращения к базе: два листа — это
свойство файлов, а не схемы.
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader


def leaves_per_app(graph: MigrationGraph) -> dict[str, list[str]]:
    """{app: [имена листьев]} — то, что ``migrate`` увидит как конфликт при len > 1."""

    out: dict[str, list[str]] = defaultdict(list)
    for app_label, name in graph.leaf_nodes():
        out[app_label].append(name)
    return dict(out)


def test_every_app_has_exactly_one_migration_leaf() -> None:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    leaves = leaves_per_app(loader.graph)

    # Присутствие впереди отсутствия: граф загружен и в нём есть приложения.
    assert "appointments" in leaves, "граф пуст или загружен не оттуда — сторож смотрит не туда"

    forked = {app: names for app, names in leaves.items() if len(names) != 1}
    assert forked == {}, (
        "у приложения больше одного листа миграций — на пилоте это crashloop web "
        f"(«Conflicting migrations», 11.09.2026): {forked}. "
        "Нужна merge-миграция: python manage.py makemigrations --merge <app>."
    )


def test_the_counter_itself_sees_a_fork() -> None:
    """Положительный контроль: сторож, разучившийся видеть вилку, зеленел бы
    на любом графе — и «одна голова» было бы неотличимо от «не проверялось»."""

    g = MigrationGraph()
    g.add_node(("app", "0001"), None)
    g.add_node(("app", "0002_a"), None)
    g.add_node(("app", "0002_b"), None)
    g.add_dependency("app.0002_a", ("app", "0002_a"), ("app", "0001"))
    g.add_dependency("app.0002_b", ("app", "0002_b"), ("app", "0001"))

    assert leaves_per_app(g) == {"app": ["0002_a", "0002_b"]}


@pytest.mark.parametrize("names", [["0001"], ["0001", "0002"]])
def test_the_counter_accepts_a_single_head(names: list[str]) -> None:
    g = MigrationGraph()
    prev = None
    for n in names:
        g.add_node(("app", n), None)
        if prev:
            g.add_dependency(f"app.{n}", ("app", n), ("app", prev))
        prev = n

    assert leaves_per_app(g) == {"app": [names[-1]]}
