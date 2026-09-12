"""`.github/scripts/shard_sum.py` — сумма junit'ов шардов против переписи (DRF-1704).

Скрипт — сторож gate-job'а `test`, и сторож обязан быть под тестом: неверно
читающий сторож хуже отсутствующего. Здесь он вызывается как процесс, с
настоящими файлами, на формах, ради которых заведён:

    сумма совпала            → 0, SHARD SUM OK
    шард потерял половину    → 1, MISMATCH с числом (это то, чего не видит
                               посуточный сентинел tests>0)
    шард потерян целиком     → 1 (сумма меньше)
    тест в двух шардах       → 1 (сумма больше)
    перепись 0 / нечитаема   → 1, CENSUS EMPTY / UNREADABLE
    junit нечитаем           → 1, JUNIT UNREADABLE
    tests печатается раньше failures в каждой строке шарда
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "shard_sum.py"


def _junit(path: Path, tests: int, failures: int = 0) -> Path:
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="0" skipped="0" time="1.0"></testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _run(census: Path, *junits: Path) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPT), str(census), *map(str, junits)],
        capture_output=True, text=True, encoding="utf-8",
    )
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def census(tmp_path: Path) -> Path:
    p = tmp_path / "catalog-census.txt"
    p.write_text("4174\n", encoding="utf-8")
    return p


def test_matching_sum_is_ok(tmp_path: Path, census: Path) -> None:
    js = [_junit(tmp_path / f"j{i}.xml", n) for i, n in enumerate((973, 716, 752, 1733))]
    code, out = _run(census, *js)
    assert code == 0, out
    assert "SHARD SUM OK" in out
    assert "sum=4174 census=4174" in out


def test_a_shard_that_lost_half_its_tests_is_refused(tmp_path: Path, census: Path) -> None:
    """Четыре зелёных junit'а, сумма короче на 866 — ровно то, чего не видит tests>0."""
    js = [_junit(tmp_path / f"j{i}.xml", n) for i, n in enumerate((973, 716, 752, 1733 - 866))]
    code, out = _run(census, *js)
    assert code == 1, out
    assert "SHARD SUM MISMATCH" in out and "-866" in out


def test_a_missing_shard_is_refused(tmp_path: Path, census: Path) -> None:
    js = [_junit(tmp_path / f"j{i}.xml", n) for i, n in enumerate((973, 716, 752))]
    code, out = _run(census, *js)
    assert code == 1, out
    assert "SHARDS 3 sum=2441 census=4174" in out


def test_a_test_counted_twice_is_refused(tmp_path: Path, census: Path) -> None:
    js = [_junit(tmp_path / f"j{i}.xml", n) for i, n in enumerate((973, 716, 752, 1733 + 1))]
    code, out = _run(census, *js)
    assert code == 1, out
    assert "+1" in out


@pytest.mark.parametrize("text", ["0\n", "", "CENSUS UNREADABLE\n"])
def test_an_empty_or_unreadable_census_cannot_gate(tmp_path: Path, text: str) -> None:
    census = tmp_path / "c.txt"
    census.write_text(text, encoding="utf-8")
    code, out = _run(census, _junit(tmp_path / "j.xml", 5))
    assert code == 1, out
    assert "CENSUS" in out


def test_an_unreadable_junit_is_refused(tmp_path: Path, census: Path) -> None:
    bad = tmp_path / "j.xml"
    bad.write_text("<not xml", encoding="utf-8")
    code, out = _run(census, bad)
    assert code == 1, out
    assert "JUNIT UNREADABLE" in out


def test_tests_is_printed_before_failures_on_every_shard_line(tmp_path: Path, census: Path) -> None:
    js = [_junit(tmp_path / f"j{i}.xml", n, failures=1) for i, n in enumerate((973, 716, 752, 1733))]
    _, out = _run(census, *js)
    for line in (ln for ln in out.splitlines() if ln.startswith("j")):
        assert line.index("tests=") < line.index("failures="), line
