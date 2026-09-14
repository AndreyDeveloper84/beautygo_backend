"""Отчёт: строки → текст/JSON. Счётчики — из тех же строк (``Summary.of``)."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from services.mapping.types import ResolverRow, Summary


def _json_default(o):
    return str(o)


def rows_to_json(rows: list[ResolverRow]) -> str:
    return json.dumps(
        {"summary": asdict(Summary.of(rows)), "rows": [asdict(r) for r in rows]},
        ensure_ascii=False, indent=2, default=_json_default,
    )


def write_json(rows: list[ResolverRow], path: Path) -> None:
    path.write_text(rows_to_json(rows), encoding="utf-8")


def row_lines(r: ResolverRow) -> list[str]:
    head = f"{r.decision.value:<17} {r.reason.value:<24} {r.raw_name}"
    if r.hint:
        head += f"   [{r.hint}]"
    lines = [head]
    if r.flags:
        lines.append(f"    флаги: {', '.join(r.flags)}")
    for c in r.candidates:
        auto = f" ← {c.rule.value}" if c.rule else ""
        lines.append(
            f"    кандидат: {c.canonical_code or '—'} «{c.name}» ({c.category_name}) "
            f"{c.evidence_type.value} lifecycle={c.lifecycle} rhc={c.requires_health_check}{auto}"
        )
    lines.append(f"    сейчас: {r.before.mapping_status}, template={r.before.template_id or '—'}")
    if r.provenance_plan:
        p = r.provenance_plan
        lines.append(f"    план: rule={p.confirmed_rule} v={p.rule_version} source_ref={p.source_ref}")
    return lines


def summary_lines(rows: list[ResolverRow]) -> list[str]:
    s = Summary.of(rows)
    out = [f"строк: {s.total}"]
    for k in sorted(s.by_decision):
        out.append(f"  {k:<17} {s.by_decision[k]}")
    for k in sorted(s.by_reason):
        out.append(f"    {k:<44} {s.by_reason[k]}")
    return out
