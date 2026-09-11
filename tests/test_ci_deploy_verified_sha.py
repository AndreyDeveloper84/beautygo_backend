"""Job ``deploy`` ставит на хост ПРОВЕРЕННЫЙ SHA, а не голову dev — B-5 (5.2), DRF-1615.

Падение пилота 11.09.2026. Прогон #330 (push ``d40d4af8``, ``test`` зелёный)
простоял в очереди раннеров 40 минут и запустил ``deploy`` в 15:49 UTC. Шаг
1/4 делал ``git reset --hard origin/dev`` — и взял голову dev на тот момент,
``683e76fb``, с двумя листьями миграций ``0018``. Entrypoint web упал на
``migrate`` → crashloop, admin 502. Зелёный ``test`` был у одного коммита,
выложен был другой.

Что держат эти тесты: ``DEPLOY_SHA`` идёт из ``github.sha`` прогона и
доезжает до хоста через ``envs``; на хосте нет ``reset --hard origin/dev``;
до сброса — проверка формы и принадлежности dev; после — SHA дерева
печатается рядом с проверенным и расхождение является отказом.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _code(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _index(lines: list[str], needle: str) -> int:
    return next((i for i, ln in enumerate(lines) if needle in ln), -1)


@pytest.fixture(scope="module")
def sync_step() -> dict:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = doc["jobs"]["deploy"]["steps"]
    step = next((s for s in steps if "1/4" in (s.get("name") or "")), None)
    assert step is not None, "шаг 1/4 (sync source) не найден — сторож смотрит не туда"
    return step


def test_the_sha_comes_from_the_run_and_reaches_the_host(sync_step: dict) -> None:
    env = sync_step.get("env") or {}
    assert "github.sha" in str(env.get("DEPLOY_SHA", "")), "DEPLOY_SHA не из github.sha прогона"
    envs = str((sync_step.get("with") or {}).get("envs", ""))
    assert "DEPLOY_SHA" in envs.split(","), "DEPLOY_SHA не передан на хост через envs — там он пуст"


def test_the_host_resets_to_the_verified_sha_never_to_the_head_of_dev(sync_step: dict) -> None:
    lines = _code(sync_step["with"]["script"])

    # Присутствие впереди отсутствия.
    resets = [ln for ln in lines if "git reset --hard" in ln]
    assert resets == ['git reset --hard "$DEPLOY_SHA"'], f"сброс дерева: {resets}"


def test_form_and_ancestry_are_checked_before_the_reset_and_the_tree_after(sync_step: dict) -> None:
    lines = _code(sync_step["with"]["script"])
    form = _index(lines, "[0-9a-f]{40}")
    ancestor = _index(lines, "merge-base --is-ancestor")
    reset = _index(lines, 'git reset --hard "$DEPLOY_SHA"')
    tree = _index(lines, "TREE_HEAD=")

    assert -1 not in (form, ancestor, reset, tree), (form, ancestor, reset, tree)
    assert form < ancestor < reset < tree, (
        f"порядок: форма {form} < ancestry {ancestor} < reset {reset} < TREE_HEAD {tree}"
    )
    assert any("TREE_HEAD" in ln and "DEPLOY_SHA" in ln and "::notice::" in ln for ln in lines), (
        "SHA дерева не печатается рядом с проверенным"
    )
    assert any("TREE_HEAD" in ln and "DEPLOY_SHA" in ln and "exit 1" in ln for ln in lines), (
        "расхождение дерева с проверенным SHA — не отказ"
    )
