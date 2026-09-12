"""Два исхода у job `deploy`: выкладка упала — или приложение выложено, упал шаг после (DRF-1661).

Run 34659054414 (00:28 12.09.2026): шаги 1/4..4/4 прошли, smoke зелёный,
упал «Publish surface state (catalog)» — и в Telegram ушло «dev deploy
failed» при живом пилоте. Владелец: нельзя слать «выкладка упала», если
приложение выложено. Отсюда два исхода:

    DEPLOYMENT_FAILED                — smoke не зелёный; упал один из 1/4..4/4
    POST_DEPLOY_PUBLICATION_FAILED   — smoke зелёный; упало что-то после

Классификация живёт в самом `run:` шага уведомления, ДО проверки секретов,
и печатает строку `outcome=…`. Здесь этот скрипт **исполняется** bash'ем с
подставленными `steps.<id>.outcome` — тест читает поведение, а не текст.
Без bash (нет на машине) тесты поведения пропускаются и говорят об этом;
структурные — идут всегда.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
NOTIFY_STEP = "Notify on failure (Telegram, runner-side)"
DEPLOY_STEP_IDS = ("sync", "build", "restart", "smoke")
PUBLISH_ID = "publish_surface_state"
BASH = shutil.which("bash")


def _deploy_steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]


@pytest.fixture(scope="module")
def notify() -> dict:
    found = [s for s in _deploy_steps() if s.get("name") == NOTIFY_STEP]
    assert len(found) == 1, found
    return found[0]


# ─── структура: у шагов есть id, уведомление их читает ──────────────────────


def test_deploy_and_publish_steps_carry_the_ids_the_notifier_reads() -> None:
    ids = {s.get("id") for s in _deploy_steps() if s.get("id")}
    missing = set(DEPLOY_STEP_IDS) | {PUBLISH_ID}
    assert missing <= ids, f"нет id у шагов: {sorted(missing - ids)}"


def test_the_publish_step_comes_after_smoke() -> None:
    """Иначе «smoke зелёный ⇒ выложено» ничего не говорит о публикации."""
    order = [s.get("id") for s in _deploy_steps() if s.get("id")]
    assert order.index("smoke") < order.index(PUBLISH_ID), order


def test_the_notifier_reads_every_step_outcome_through_env(notify: dict) -> None:
    env = notify.get("env") or {}
    for step_id in (*DEPLOY_STEP_IDS, PUBLISH_ID):
        expected = f"${{{{ steps.{step_id}.outcome }}}}"
        assert expected in env.values(), f"steps.{step_id}.outcome не передаётся в env шага уведомления"
    assert notify.get("if") == "failure()"


# ─── поведение: скрипт исполняется с подставленными исходами ────────────────


def _run_notifier(notify: dict, outcomes: dict[str, str]) -> str:
    """Выполнить `run:` шага без секретов — отправки нет, классификация есть."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_SHA": "0123456789abcdef",
        "GITHUB_RUN_ID": "1",
        "GITHUB_REPOSITORY": "x/y",
        "STEP_SYNC": outcomes.get("sync", "success"),
        "STEP_BUILD": outcomes.get("build", "success"),
        "STEP_RESTART": outcomes.get("restart", "success"),
        "STEP_SMOKE": outcomes.get("smoke", "success"),
        "STEP_PUBLISH_SURFACE_STATE": outcomes.get(PUBLISH_ID, "success"),
    }
    r = subprocess.run(
        [BASH, "-c", notify["run"]], env=env, capture_output=True, text=True, encoding="utf-8"
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


needs_bash = pytest.mark.skipif(BASH is None, reason="bash не найден — поведение уведомления не проверено")


@needs_bash
def test_publication_failure_after_green_smoke_says_the_app_is_deployed(notify: dict) -> None:
    """Ровно случай 00:28: 1/4..4/4 зелёные, surface_state красный."""
    out = _run_notifier(notify, {PUBLISH_ID: "failure"})

    assert "outcome=POST_DEPLOY_PUBLICATION_FAILED" in out, out
    assert "приложение выложено" in out
    assert "Publish surface state" in out
    assert "dev deploy failed" not in out


@needs_bash
@pytest.mark.parametrize("failed", DEPLOY_STEP_IDS)
def test_a_red_deploy_step_is_deployment_failed_and_names_the_step(notify: dict, failed: str) -> None:
    """Упал шаг выкладки — последующие skipped, smoke не success."""
    outcomes = {failed: "failure"}
    for later in DEPLOY_STEP_IDS[DEPLOY_STEP_IDS.index(failed) + 1:]:
        outcomes[later] = "skipped"
    outcomes[PUBLISH_ID] = "skipped"
    out = _run_notifier(notify, outcomes)

    assert "outcome=DEPLOYMENT_FAILED" in out, out
    assert "dev deploy failed" in out
    labels = {"sync": "1/4 sync", "build": "2/4 build", "restart": "3/4 restart", "smoke": "4/4 smoke"}
    assert f"failed_step={labels[failed]}" in out
    assert "приложение выложено" not in out


@needs_bash
def test_an_unclassified_failure_after_green_smoke_is_still_post_deploy(notify: dict) -> None:
    """Smoke зелёный, surface_state тоже, а job красный (шаг без id) — всё равно «выложено»."""
    out = _run_notifier(notify, {})

    assert "outcome=POST_DEPLOY_PUBLICATION_FAILED" in out, out
    assert "не surface_state" in out


@needs_bash
def test_the_classification_does_not_depend_on_the_secrets(notify: dict) -> None:
    """Строка outcome= печатается ДО проверки секретов — иначе на репо без
    секретов классификацию не увидеть ни в логе, ни здесь."""
    out = _run_notifier(notify, {PUBLISH_ID: "failure"})
    assert out.index("outcome=") < out.index("skipping alert")
