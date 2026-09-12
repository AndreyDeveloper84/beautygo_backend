"""Раскладка шардов в ci.yml: без пересечений, без дыр, gate на месте (DRF-1704).

Четыре шарда — четыре списка путей, и три способа потерять тесты молча:
путь в двух шардах (сумма больше переписи), путь ни в одном (меньше),
и ignore-список «всего остального», отставший от явных списков (и то и
другое). Gate-job `test` ловит это по сумме junit'ов ПОСЛЕ прогона; этот
сторож ловит до — по YAML, за секунду, на PR.

Отдельно стережётся форма: имя gate-job'а `test` (его требует branch
protection вместе с `lint`), `deploy` зависит от него, каждый шард пишет и
проверяет СВОЙ junit, а «всё остальное» — вычитание, чтобы новый app
попадал в шард автоматически.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
IGNORE = "--ignore="


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _shards(workflow: dict) -> list[dict]:
    return workflow["jobs"]["test-shard"]["strategy"]["matrix"]["shard"]


def _split(shards: list[dict]) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """({id: явные пути}, {id: ignore-пути шарда-вычитания})."""
    explicit: dict[int, set[str]] = {}
    rest: dict[int, set[str]] = {}
    for sh in shards:
        args = shlex.split(str(sh["paths"]))
        ignores = {a[len(IGNORE):] for a in args if a.startswith(IGNORE)}
        if ignores:
            assert args[0] == ".", f"шард {sh['id']}: вычитание обязано идти от `.`, не от {args[0]!r}"
            rest[int(sh["id"])] = ignores
        else:
            explicit[int(sh["id"])] = set(args)
    return explicit, rest


def _layout_problems(shards: list[dict]) -> list[str]:
    explicit, rest = _split(shards)
    problems: list[str] = []
    if len(rest) != 1:
        problems.append(f"шардов-вычитаний {len(rest)}, нужен ровно один")
    ids = sorted(explicit)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            both = explicit[a] & explicit[b]
            if both:
                problems.append(f"пути в двух шардах ({a} и {b}): {sorted(both)}")
    union = set().union(*explicit.values()) if explicit else set()
    for rid, ignores in rest.items():
        if ignores != union:
            problems.append(
                f"ignore-список шарда {rid} отстал от явных: лишние {sorted(ignores - union)}, "
                f"недостающие {sorted(union - ignores)}"
            )
    return problems


# ─── раскладка ──────────────────────────────────────────────────────────────


def test_shards_partition_without_overlap_or_gaps() -> None:
    shards = _shards(_workflow())
    assert len(shards) == 4, [s["id"] for s in shards]
    assert _layout_problems(shards) == []


def test_every_explicit_path_is_a_directory_with_tests() -> None:
    """Устаревший селектор — pytest падает с exit 4, но лучше назвать путь здесь."""
    explicit, _ = _split(_shards(_workflow()))
    for sid, paths in explicit.items():
        for p in paths:
            d = REPO / p
            assert d.is_dir(), f"шард {sid}: {p} не каталог"
            assert any(d.rglob("test_*.py")), f"шард {sid}: в {p} нет test_*.py — шард считает пустоту"


def test_the_layout_covers_every_top_level_app_that_has_tests() -> None:
    """Перепись предмета: каждый каталог с тестами либо назван явно, либо остаётся в вычитании."""
    explicit, rest = _split(_shards(_workflow()))
    named = set().union(*explicit.values())
    with_tests = {
        p.name for p in REPO.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in {"venv", "node_modules"}
        and any(p.rglob("test_*.py"))
    }
    assert with_tests, "ни одного каталога с тестами — измерялся не тот корень"
    assert named <= with_tests, sorted(named - with_tests)
    # Всё, что не названо, покрыто вычитанием по построению (`.` минус
    # явные). Равенство ignore-списка явным стережёт ОДИН тест —
    # test_shards_partition_without_overlap_or_gaps, — не этот: два сторожа
    # на одно условие ловят подмену друг друга, и проба перестаёт считаться.
    assert len(rest) == 1


# ─── форма job'ов ───────────────────────────────────────────────────────────


def test_the_gate_keeps_the_required_name_and_deploy_needs_it() -> None:
    jobs = _workflow()["jobs"]
    assert "lint" in jobs and "test" in jobs, "branch protection требует job'ы lint и test"
    needs = jobs["test"]["needs"]
    assert set(needs) == {"test-shard", "checks"}, needs
    assert jobs["test"].get("if") == "always()", "gate обязан дойти до суммы и при красном шарде"
    assert jobs["deploy"]["needs"] == "test"


def test_each_shard_writes_checks_and_uploads_its_own_junit() -> None:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["test-shard"]["steps"]}
    run = steps["Run tests"]["run"]
    assert "--junitxml=junit-catalog-${{ matrix.shard.id }}.xml" in run
    assert run.rstrip().endswith("${{ matrix.shard.paths }}"), run
    sentinel_file = steps["pytest test counts (junit sentinel)"]["env"]["JUNIT_FILE"]
    assert sentinel_file == "junit-catalog-${{ matrix.shard.id }}.xml"
    up = steps["Upload pytest junit"]["with"]
    assert up["path"] == "junit-catalog-${{ matrix.shard.id }}.xml"
    assert up["name"] == "junit-catalog-${{ matrix.shard.id }}-${{ github.sha }}"


def test_the_gate_sums_with_the_tested_script_over_all_shard_junits() -> None:
    steps = {s.get("name"): s for s in _workflow()["jobs"]["test"]["steps"]}
    run = steps["Sum shard junits against the census"]["run"]
    assert "python3 .github/scripts/shard_sum.py artifacts/catalog-census.txt artifacts/junit-catalog-*.xml" in run
    assert (REPO / ".github" / "scripts" / "shard_sum.py").is_file()
    dl = steps["Download shard junits and the census"]["with"]
    assert dl["pattern"] == "*-${{ github.sha }}" and dl.get("merge-multiple") is True


def test_the_census_runs_unsharded_in_checks() -> None:
    names = [s.get("name") for s in _workflow()["jobs"]["checks"]["steps"]]
    assert "collection census (sentinel for the shard layout)" in names
    assert "Upload collection census" in names
    assert "collection census (sentinel for the shard layout)" not in [
        s.get("name") for s in _workflow()["jobs"]["test-shard"]["steps"]
    ], "перепись внутри шарда считала бы только его часть"


# ─── положительный контроль ─────────────────────────────────────────────────


def _s(*paths: str) -> list[dict]:
    return [{"id": i + 1, "paths": p} for i, p in enumerate(paths)]


@pytest.mark.parametrize(
    ("shards", "expect"),
    [
        (_s("a b", "c", ". --ignore=a --ignore=b --ignore=c"), []),
        (_s("a b", "b", ". --ignore=a --ignore=b"), ["двух шардах"]),
        (_s("a b", "c", ". --ignore=a --ignore=b"), ["отстал"]),
        (_s("a", ". --ignore=a", ". --ignore=a"), ["ровно один"]),
        (_s("a", "b"), ["ровно один"]),
    ],
)
def test_the_layout_checker_names_each_way_to_lose_tests(shards: list[dict], expect: list[str]) -> None:
    problems = _layout_problems(shards)
    assert len(problems) == len(expect), problems
    for word, problem in zip(expect, problems):
        assert word in problem, problem
