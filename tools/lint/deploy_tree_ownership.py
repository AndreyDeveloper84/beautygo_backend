#!/usr/bin/env python3
"""Count objects in the catalog deploy tree that the deploy user does not own.

Ported from ai-bot-platform `tools/lint/deploy_tree_ownership.py` (DRF-1646)
on 2026-09-12 — the mechanics are the same; the facts below are this tree's.

# The defect, in this repository

The catalog runs its containers from an image with sources baked in and only
`staticfiles/`, `media/` and `firebase-admin.json` bind-mounted from
`/home/taximeter/beautygo/dev` on `ruvds-o1mqo` / 176.119.159.141. Before
#354 the containers ran as root (no `USER` in the Dockerfile, no `user:` in
compose), so everything `collectstatic` wrote into the mounted `staticfiles/`
landed on the host owned by root: 197 objects measured 2026-09-11 13:46 UTC,
all in `staticfiles/`, 0 root-owned directories.

#354 put `user: 1000:1000` on web/celery. The next deploy's `collectstatic`
then rewrote every static file it copied AS the tree owner — replacement in a
taximeter-owned directory is the directory's right, not the file's — and the
197 collapsed to **4** (the `unfold/*/simplebar/*` files collectstatic saw as
unmodified and left alone). Measured 2026-09-12 02:20 UTC, 2367 objects
walked. That is the baseline: a residue collectstatic will never touch until
those upstream files change, not a rate.

# What this guard therefore does NOT cover — named, so a silent limit is not
# read as a closed border

It counts. It cannot attribute, and it cannot prevent. A green run means "no
new foreign objects since the baseline", not "only one account writes here".
A person with a root shell in the tree is invisible to it until the next run.

# A ratchet, not a verdict

The baseline is the measured number. The guard fails when a bucket **grows**;
shrinking is the repair, and the baseline is then lowered by hand. Per bucket,
never on the total: a repair in `staticfiles/` and a regression in `.git/`
cannot cancel out into a green sum.

# Why the walk and the judgement are separate functions

`classify()` is pure — `(path, uid)` pairs plus the expected uid in, a
breakdown out. `walk()` does the `stat`. On Windows `st_uid` is 0 for
everything, so a guard that measured and judged in one pass could only be
tested on the box it guards. The pure half is testable anywhere, including
the positive control that matters: an expected uid that owns nothing must
make every entry foreign — otherwise "0 foreign" is equally consistent with
"clean" and "cannot tell foreign from own".
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: Buckets, in the order the report prints them. `OTHER` is last and is not a
#: leftover: it is where source files landed, and it is the bucket whose
#: producer this repository does not explain.
GIT = ".git/"
STATICFILES = "staticfiles/"
MEDIA = "media/"
PYCACHE = "__pycache__/"
OTHER = "other"

CATEGORIES: tuple[str, ...] = (GIT, STATICFILES, MEDIA, PYCACHE, OTHER)

BASELINE_PATH = Path(__file__).with_name("deploy_tree_ownership_baseline.txt")


@dataclass(frozen=True)
class Entry:
    """One filesystem object and the uid that owns it."""

    path: str
    uid: int


def categorise(path: str) -> str:
    """Which bucket a path belongs to. Pure, and deliberately not clever.

    Matching is on path segments rather than substrings: a directory named
    `staticfiles_old` is not `staticfiles/`, and a source file whose name
    contains `.git` is not git bookkeeping.
    """

    parts = PurePosixPath(path).parts
    if ".git" in parts:
        return GIT
    if "__pycache__" in parts or path.endswith(".pyc"):
        return PYCACHE
    if parts and parts[0] == "staticfiles":
        return STATICFILES
    if parts and parts[0] == "media":
        return MEDIA
    return OTHER


def classify(entries: list[Entry], *, expected_uid: int) -> dict[str, int]:
    """Count entries NOT owned by `expected_uid`, per category.

    Returns every category, including the ones at zero: a report that omits
    empty buckets cannot be told apart from a report that never looked at them.
    """

    counted: Counter[str] = Counter()
    for entry in entries:
        if entry.uid != expected_uid:
            counted[categorise(entry.path)] += 1
    return {category: counted.get(category, 0) for category in CATEGORIES}


def walk(root: Path) -> list[Entry]:
    """Every object under `root`, with its owning uid, paths relative to root.

    Directories are counted as objects too — a root-owned directory is exactly
    what stops the deploy user creating a file inside it, which is the failure
    this whole thing is about.
    """

    entries: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            full = Path(dirpath) / name
            try:
                uid = full.lstat().st_uid
            except OSError:
                # Unreadable is not "fine": say so and keep going, so one bad
                # object cannot silence the count for the whole tree.
                print(f"::warning::не удалось прочитать владельца: {full}", file=sys.stderr)
                continue
            entries.append(Entry(path=full.relative_to(root).as_posix(), uid=uid))
    return entries


def read_baseline(path: Path = BASELINE_PATH) -> dict[str, int]:
    """The recorded counts. A missing or unparsable file is an error, not zero.

    Reading an absent baseline as all-zeros would turn "nobody has measured
    this yet" into "the tree is clean" — the loudest possible version of the
    defect this file exists to prevent.
    """

    if not path.exists():
        raise SystemExit(f"baseline не найден: {path}")
    recorded: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        category, _, count = line.partition("=")
        recorded[category.strip()] = int(count.strip())
    missing = [c for c in CATEGORIES if c not in recorded]
    if missing:
        raise SystemExit(f"в baseline нет категорий: {missing}")
    return recorded


def report(found: dict[str, int], baseline: dict[str, int]) -> tuple[str, bool]:
    """Render the comparison and say whether it is a failure.

    Per category, never on the total: a repair in one bucket and a regression
    in another must not cancel into a green sum.
    """

    lines = [f"{'категория':<16}{'сейчас':>8}{'храповик':>10}{'':>4}"]
    grew: list[str] = []
    for category in CATEGORIES:
        now, was = found[category], baseline[category]
        mark = ""
        if now > was:
            mark = "  ВЫРОСЛО"
            grew.append(f"{category}: {was} → {now}")
        elif now < was:
            mark = "  меньше — опустите храповик"
        lines.append(f"{category:<16}{now:>8}{was:>10}{mark}")
    lines.append(f"{'ВСЕГО':<16}{sum(found.values()):>8}{sum(baseline.values()):>10}")
    if grew:
        lines.append("")
        lines.append("::error::чужих объектов стало больше: " + "; ".join(grew))
        lines.append(
            "Новый чужой объект — это либо контейнер не под user: владельца "
            "(docker-compose.yml: web/celery_* под APP_UID), либо человек, "
            "работавший в дереве под root."
        )
    return "\n".join(lines), bool(grew)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="корень дерева выкладки")
    # Обязательный, без умолчания «тот, под кем запущено». Умолчание звучало
    # бы удобно и сравнивало бы дерево пилота с учётной записью того, кто
    # запустил проверку, — на раннере это чужой uid, и сторож объявил бы чужим
    # ВСЁ дерево, зелено отчитавшись о работе. Владельца называют вслух.
    parser.add_argument(
        "--expected-uid",
        type=int,
        required=True,
        help="uid учётки выкладки — того, кто ДОЛЖЕН владеть деревом",
    )
    args = parser.parse_args(argv)

    expected_uid = args.expected_uid
    # Печатаем предмет рядом с результатом: «проверка отработала» и «проверка
    # смотрела на то дерево и на ту учётку» — разные утверждения.
    print(f"дерево: {args.root}   ожидаемый владелец uid={expected_uid}")

    entries = walk(args.root)
    if not entries:
        print(f"::error::под {args.root} не найдено ни одного объекта — мерили не то дерево")
        return 1

    found = classify(entries, expected_uid=expected_uid)
    text, failed = report(found, read_baseline())
    print(f"объектов всего: {len(entries)}")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
