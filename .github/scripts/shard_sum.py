"""Sum the shard junits and refuse unless the sum equals the collection census.

Second half of #352 (DRF-1704). Each shard already refuses `tests == 0` on
its own junit — that catches a shard that lost ALL of its tests. It does not
catch a shard that lost half of them, and it does not catch a path that fell
out of every shard's selector at once: four green junits whose sum is short
by 300 tests look exactly like four green junits. The census (the number
`pytest --collect-only` reported on the same checkout, unsharded) is the
only figure the sum can be checked against.

Usage:  shard_sum.py CENSUS_FILE JUNIT_FILE...

Exit 1 with a one-line reason on: unreadable census, census == 0, fewer
than one junit, an unreadable junit, or sum != census. The per-shard line
prints `tests` BEFORE `failures`, for the same reason as the sentinel: a
`failures=0` next to `tests=0` must read as «did not run», not «green».

WHY THIS IS A FILE AND NOT A HEREDOC IN ci.yml: it is a guard, and a guard
has to be under test itself (tests/test_shard_sum.py feeds it real junit
shapes — including the one where a shard silently lost half its tests).
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read_census(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"CENSUS MISSING - {path}: {exc}")
    try:
        value = int(text.split()[0])
    except (IndexError, ValueError):
        raise SystemExit(f"CENSUS UNREADABLE - {path!s} holds {text[:40]!r}, not a number")
    if value <= 0:
        raise SystemExit(f"CENSUS EMPTY - {path} says {value}: an empty census cannot gate anything")
    return value


def read_junit(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise SystemExit(f"JUNIT UNREADABLE - {path}: {exc}")
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise SystemExit(f"JUNIT UNREADABLE - no <testsuite> in {path}")
    return {k: int(suite.get(k) or 0) for k in ("tests", "failures", "errors", "skipped")}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: shard_sum.py CENSUS_FILE JUNIT_FILE...")
        return 2
    census = read_census(Path(argv[1]))
    junits = [Path(a) for a in argv[2:]]
    total = 0
    for junit in junits:
        counts = read_junit(junit)
        print(
            f"{junit.name}: tests={counts['tests']} failures={counts['failures']} "
            f"errors={counts['errors']} skipped={counts['skipped']}"
        )
        total += counts["tests"]
    print(f"SHARDS {len(junits)} sum={total} census={census}")
    if total != census:
        print(
            f"SHARD SUM MISMATCH - the shards ran {total} tests, the census collected {census} "
            f"({total - census:+d}). A test is in no shard, in two shards, or a shard ran short."
        )
        return 1
    print("SHARD SUM OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
