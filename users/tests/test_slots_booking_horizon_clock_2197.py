"""Узел h3 DRF-2081 не зависит от того, КОГДА его собрали и КОГДА запустили (DRF-2197).

Флейк: ``test_slots_booking_horizon_2081`` выбирал пояс ``TZ`` по часу UTC
**при сборе модуля** (``_midday_zone()`` на уровне модуля), а мгновение
горизонта считал по часам **при выполнении**. Собрали в 12:59:59 UTC,
запустили в 13:00:01 — посылка h3 «мгновение горизонта ≈ полдень местного»
ломается, узел красен без дефекта в продукте. Ловили дважды независимо
(в т.ч. 13:10 по Etc/GMT-5 против границы 13:00).

Часы — молчаливый параметр: чинится фиксацией ВРЕМЕНИ, а не подбором пояса.

* c1 — модуль узла, собранный в 12:59:59 UTC, держит посылку h3 и в
  13:00:01 UTC (красное до правки — на границе часа);
* c2 — то же через полночь UTC (23:59:59 → 00:00:01): день тоже не
  должен уезжать;
* c3 — на уровне модуля часы не читаются вовсе (перепись по AST: ни одного
  вызова ``now`` вне функций) — класс дефекта, а не один случай.
"""

from __future__ import annotations

import ast
import importlib
from datetime import time
from pathlib import Path

import pytest
from freezegun import freeze_time

MODULE = "users.tests.test_slots_booking_horizon_2081"


def _premise_holds_when_collected_at(collected: str, run: str) -> bool:
    """Собрать модуль узла в ``collected``, проверить посылку h3 в ``run``."""
    module = importlib.import_module(MODULE)
    try:
        with freeze_time(collected):
            module = importlib.reload(module)
        with freeze_time(run):
            end_local = module._horizon_end_local()
        return time(11, 0) <= end_local.time() < time(13, 0)
    finally:
        importlib.reload(module)


@pytest.mark.parametrize(
    ("collected", "run"),
    [
        ("2026-09-21T12:59:59Z", "2026-09-21T13:00:01Z"),
        ("2026-09-21T06:59:59Z", "2026-09-21T07:10:00Z"),
    ],
    ids=["noon-utc-boundary", "13-10-at-gmt-minus-5"],
)
def test_c1_the_premise_survives_an_hour_boundary(collected: str, run: str) -> None:
    assert _premise_holds_when_collected_at(collected, run)


def test_c2_the_premise_survives_midnight_utc() -> None:
    assert _premise_holds_when_collected_at("2026-09-21T23:59:59Z", "2026-09-22T00:00:01Z")


def test_c3_the_module_reads_no_clock_at_import() -> None:
    path = Path(importlib.import_module(MODULE).__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    inside = {id(n) for f in functions for n in ast.walk(f)}

    def _callee(call: ast.Call) -> str:
        return getattr(call.func, "attr", getattr(call.func, "id", ""))

    # Функции модуля, которые сами читают часы: вызвать такую на уровне
    # модуля — то же, что прочитать часы при сборе (так и был устроен флейк:
    # ``TZ = _midday_zone()``).
    clock_readers = {"now", "utcnow", "today"} | {
        f.name
        for f in functions
        if any(isinstance(n, ast.Call) and _callee(n) in ("now", "utcnow", "today") for n in ast.walk(f))
    }
    module_level_clock_reads = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _callee(n) in clock_readers and id(n) not in inside
    ]
    # Присутствие: в модуле есть функции, и перепись их видит.
    assert functions
    assert module_level_clock_reads == [], module_level_clock_reads
