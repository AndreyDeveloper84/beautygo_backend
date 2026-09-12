"""Форма шага «храповик чужих владельцев» в job `deploy` (DRF-1677, класс DRF-1646).

Сам храповик — `tools/lint/deploy_tree_ownership.py`, под тестом в
`tests/tools/`. Здесь стережётся, что job его ЗОВЁТ, и зовёт верно:
после smoke (краснота — «после выкладки», не «выкладка»), из дерева
выкладки, с uid ДЕРЕВА (stat), не зовущего (`id -u` под root даёт 0 —
мера, которая работает ровно как написана и ничего не делает), и печатает
актора числом рядом с результатом.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Deploy-tree ownership ratchet (catalog)"
TOOL = "tools/lint/deploy_tree_ownership.py"


def _deploy_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]


def _code(step: dict) -> str:
    script = (step.get("with") or {}).get("script") or ""
    return "\n".join(ln for ln in script.splitlines() if ln.strip() and not ln.strip().startswith("#"))


@pytest.fixture(scope="module")
def step() -> dict:
    found = [s for s in _deploy_steps() if s.get("name") == STEP_NAME]
    assert len(found) == 1, f"шаг {STEP_NAME!r}: найдено {len(found)}"
    return found[0]


def test_the_ratchet_runs_after_smoke_and_before_publication(step: dict) -> None:
    names = [s.get("name") for s in _deploy_steps()]
    smoke = next(i for i, n in enumerate(names) if n and n.startswith("4/4 Smoke"))
    assert names.index(STEP_NAME) > smoke, "храповик до smoke красил бы «выкладку», а не «шаг после»"


def test_it_calls_the_tested_tool_from_the_deploy_tree_with_the_tree_uid(step: dict) -> None:
    code = _code(step)
    assert "cd /home/taximeter/beautygo/dev" in code
    assert re.search(r"TREE_UID=\$\(stat -c %u \.\)", code), "uid дерева не берётся через stat"
    assert re.search(rf'python3 {re.escape(TOOL)} \. --expected-uid "\$TREE_UID"', code), code
    assert "$(id -u)" not in code.split("--expected-uid")[1], "--expected-uid через id -u — под root это 0"
    assert (REPO / TOOL).is_file() and (REPO / "tools/lint/deploy_tree_ownership_baseline.txt").is_file()


def test_the_actor_is_printed_as_a_number(step: dict) -> None:
    """Имя маскируется GitHub'ом в ***; число — нет."""
    assert 'uid=$(id -u)' in _code(step)


def test_the_step_fails_the_job_when_the_ratchet_grows(step: dict) -> None:
    """Без continue-on-error и без `|| true`: рост чужих объектов — красный шаг."""
    assert not step.get("continue-on-error")
    assert "|| true" not in _code(step)
    assert "set -eo pipefail" in _code(step)
