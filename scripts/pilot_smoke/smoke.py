"""CLI вход pilot smoke-runner'а.

Запуск:
    python -m scripts.pilot_smoke.smoke [--only S1,S4] [--md report.md]

Exit codes: 0 — нет FAIL; 1 — есть FAIL; 2 — ошибка конфигурации.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from .config import SmokeConfig
from .http import FAIL, PASS, SKIP, Check, SmokeHttp
from .probes import Probes
from .scenarios import SCENARIOS, Ctx


def scenario_status(checks: list[Check]) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if PASS in statuses:
        return PASS
    return SKIP


def render_report(all_checks: dict[str, list[Check]], started: datetime, cfg: SmokeConfig) -> str:
    lines = [
        f"# Pilot smoke report — {started.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        f"- Ayla: `{cfg.ayla_base_url or '—'}`",
        f"- Bot: `{cfg.bot_base_url or '—'}`",
        f"- SQL-пробы: {'включены' if cfg.bot_db_dsn else 'выключены (нет BOT_DB_DSN)'}",
        "",
        "| # | Сценарий | Результат | Деталь |",
        "|---|----------|-----------|--------|",
    ]
    names = dict((sid, name) for sid, name, _ in SCENARIOS)
    for sid, checks in all_checks.items():
        status = scenario_status(checks)
        fails = [c for c in checks if c.status == FAIL]
        skips = [c for c in checks if c.status == SKIP]
        detail = f"{sum(1 for c in checks if c.status == PASS)} PASS"
        if fails:
            detail += f", {len(fails)} FAIL: " + "; ".join(f"{c.name} ({c.detail[:120]})" for c in fails)
        if skips:
            detail += f", {len(skips)} SKIP"
        lines.append(f"| {sid} | {names.get(sid, sid)} | {status} | {detail} |")
    lines += ["", "## Проверки", ""]
    for sid, checks in all_checks.items():
        lines.append(f"### {sid}. {names.get(sid, sid)}")
        for c in checks:
            mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}[c.status]
            lines.append(f"- {mark} **{c.name}** — {c.detail}")
        lines.append("")
    return "\n".join(lines)


def print_console(all_checks: dict[str, list[Check]]) -> None:
    names = dict((sid, name) for sid, name, _ in SCENARIOS)
    print()
    print(f"{'#':<4} {'Сценарий':<46} {'Результат':<9} Деталь")
    print("-" * 100)
    for sid, checks in all_checks.items():
        status = scenario_status(checks)
        passed = sum(1 for c in checks if c.status == PASS)
        fails = [c for c in checks if c.status == FAIL]
        skips = sum(1 for c in checks if c.status == SKIP)
        detail = f"{passed} PASS, {len(fails)} FAIL, {skips} SKIP"
        print(f"{sid:<4} {names.get(sid, sid):<46} {status:<9} {detail}")
        for c in checks:
            if c.status != PASS:
                print(f"     └ {c.status}: {c.name} — {c.detail[:160]}")
    print()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="pilot_smoke",
        description="Black-box smoke-runner пилота 2026-08-15 (acceptance §10, контракты v1.8.0)")
    parser.add_argument("--only", help="список сценариев через запятую, напр. S1,S4")
    parser.add_argument("--md", help="путь для markdown-отчёта")
    args = parser.parse_args(argv)

    cfg = SmokeConfig.from_env()
    if not cfg.ayla_base_url and not cfg.bot_base_url:
        print("ОШИБКА: задайте хотя бы AYLA_BASE_URL или BOT_BASE_URL (см. README.md)", file=sys.stderr)
        return 2

    only = {s.strip().upper() for s in args.only.split(",")} if args.only else None
    http = SmokeHttp(cfg)
    probes = Probes(cfg.bot_db_dsn)
    ctx: Ctx = Ctx()
    started = datetime.now(timezone.utc)
    all_checks: dict[str, list[Check]] = {}

    for sid, _name, fn in SCENARIOS:
        if only and sid not in only:
            continue
        checks: list[Check] = []
        t0 = time.monotonic()
        try:
            fn(cfg, http, probes, ctx, checks.append)
        except Exception as exc:  # раннер не должен падать целиком из-за одного сценария
            checks.append(Check(sid, "внутренняя ошибка сценария", FAIL, f"{type(exc).__name__}: {exc}"))
        all_checks[sid] = checks
        print(f"[{sid}] готово за {time.monotonic() - t0:.1f}s "
              f"({sum(1 for c in checks if c.status == PASS)} PASS / "
              f"{sum(1 for c in checks if c.status == FAIL)} FAIL / "
              f"{sum(1 for c in checks if c.status == SKIP)} SKIP)")

    probes.close()
    print_console(all_checks)
    report = render_report(all_checks, started, cfg)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Markdown-отчёт: {args.md}")

    return 1 if any(c.status == FAIL for cs in all_checks.values() for c in cs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
