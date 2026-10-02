"""Внести знание о процедурах из файла куратора (DRF-2717).

Читает JSON и заводит строки :class:`~services.models.ProcedureCapability`
(что процедура умеет) и :class:`~services.models.CapabilityGoalLink` (какой
цели это помогает). Второй вход рядом с формой в Django-admin; правила
основания у обоих общие — :mod:`services.knowledge_intake`.

Что команда делает
------------------
* Заводит только НОВОЕ: возможность ищется по паре (шаблон, ``key``), связь —
  по паре (возможность, цель). Существующую строку команда не трогает ни в
  одном поле — так же, как засев канона и целей после DRF-2663: всё, что
  лежит в базе, дальше живёт под рукой куратора в админке, и повторный прогон
  файла не должен возвращать подтверждённое к черновику файла. Сколько строк
  оставлено как есть — печатается.
* Статус по умолчанию — ``system_inference`` (черновик, человеку не
  говорится). ``approved`` из файла принимается только с полным основанием в
  той же строке: ``confirmed_rule`` + ``rule_version`` + ``confirmed_at`` +
  ``source_ref``. Подтверждение ЧЕЛОВЕКОМ делается в админке — в файле
  человека назвать нечем.
* Сначала проверяет файл целиком, потом пишет. Любая ошибка — неизвестный код
  шаблона, неизвестная цель, подтверждение без основания, курс голым числом —
  печатается списком, и не пишется ничего.

Чего команда не делает
----------------------
Знание не выдумывает и из названий услуг не выводит. Новое издание файла на
уже заведённые строки не ложится (см. выше) — правка существующего делается
в админке. Шаблоны и цели не заводит: им нужен ``seed_canonical_catalog`` и
``seed_goal_options``, и порядок именно такой.

Файл
----
По умолчанию — ``services/seeds/procedure_knowledge.json``. Пока куратор его не
положил, файла нет, и команда без ``--file`` честно печатает, что вносить
нечего, и завершается успехом: так её можно звать при каждой выкладке.
``--file`` с несуществующим путём — ошибка.

Формат — ``services/seeds/procedure_knowledge.example.json`` (синтетика, в
базу стенда не попадает: это не файл по умолчанию)::

    {"capabilities": [{
        "template_code": "1.1.3",          # ServiceTemplate.canonical_code
        "key": "temporary_relaxation",     # машинный ключ смысла, не из текста
        "text_client": "...", "text_professional": "...",
        "expected_effect": "...", "result_timeframe": "...",
        "claim_scope": "supported",        # | not_supported | prohibited_claim
        "limitations": "...", "evidence_source": "...", "evidence_kind": "...",
        "source_ref": "...",
        "goal_links": [{"goal": "relax",   # GoalOption.key
                        "course_pattern": "...", "variability_note": "...",
                        "result_horizon": "...", "evidence_source": "...",
                        "source_ref": "..."}]
    }]}

Usage::

    python manage.py seed_procedure_knowledge
    python manage.py seed_procedure_knowledge --dry-run
    python manage.py seed_procedure_knowledge --file <path>
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from services.knowledge_intake import APPROVED, course_errors, provenance_errors
from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    ServiceTemplate,
)

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "seeds" / "procedure_knowledge.json"

_STATUSES = {choice.value for choice in ClaimEvidence.Status}
_SCOPES = {choice.value for choice in ClaimEvidence.ClaimScope}

_CAPABILITY_TEXT = ("text_client", "text_professional", "expected_effect", "result_timeframe")
_LINK_TEXT = ("course_pattern", "result_horizon", "variability_note")
_CLAIM_TEXT = (
    "limitations", "evidence_source", "evidence_kind", "source_ref",
    "confirmed_rule", "rule_version",
)


def _text(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    return value.strip() if isinstance(value, str) else ""


class _Problems:
    """Все ошибки файла сразу — куратор правит файл за один проход, не по одной."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.lines.append(f"{where}: {message}")


def _claim_fields(row: dict[str, Any], where: str, problems: _Problems) -> dict[str, Any]:
    """Общие поля основания одной строки файла — проверенные, готовые к записи."""
    status = _text(row, "status") or ClaimEvidence.Status.SYSTEM_INFERENCE.value
    scope = _text(row, "claim_scope") or ClaimEvidence.ClaimScope.NOT_SUPPORTED.value
    if status not in _STATUSES:
        problems.add(where, f"неизвестный status «{status}» (допустимо: {sorted(_STATUSES)})")
    if scope not in _SCOPES:
        problems.add(where, f"неизвестный claim_scope «{scope}» (допустимо: {sorted(_SCOPES)})")

    fields: dict[str, Any] = {name: _text(row, name) for name in _CLAIM_TEXT}
    fields["status"] = status
    fields["claim_scope"] = scope

    for name in ("confirmed_at", "valid_until"):
        raw = row.get(name)
        fields[name] = None
        if raw in (None, ""):
            continue
        parsed = parse_datetime(raw) if isinstance(raw, str) else None
        if parsed is None or parsed.tzinfo is None:
            problems.add(where, f"{name} — нужна дата и время с часовым поясом (ISO 8601), получено «{raw}»")
        else:
            fields[name] = parsed

    rule_named = bool(fields["confirmed_rule"]) and bool(fields["rule_version"])
    for field, message in provenance_errors(
        status=status, source_ref=fields["source_ref"], has_confirmer=rule_named,
    ).items():
        if field == "status":
            message = (
                "approved из файла принимается только с названным правилом: "
                "нужны confirmed_rule и rule_version (подтверждение человеком — в админке)."
            )
        problems.add(where, f"{field} — {message}")
    if status == APPROVED and fields["confirmed_at"] is None:
        problems.add(where, "confirmed_at — у подтверждённой строки должна быть дата подтверждения.")
    if status != APPROVED:
        # Отметки подтверждения у черновика были бы утверждением, которого
        # никто не делал; из файла они в базу не едут.
        fields["confirmed_rule"] = ""
        fields["rule_version"] = ""
        fields["confirmed_at"] = None
    return fields


class Command(BaseCommand):
    help = "Seed procedure knowledge (capabilities and goal links) from the curator's file."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", default=None)
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Проверить файл и напечатать, что было бы заведено, ничего не записывая.",
        )

    def handle(self, *args, **options) -> None:
        explicit = options["file"]
        path = Path(explicit) if explicit else DEFAULT_FILE
        if not path.exists():
            if explicit:
                raise CommandError(f"Knowledge file not found: {path}")
            self.stdout.write(
                f"No knowledge file at {path} — nothing to seed. "
                "Knowledge is entered in the admin or by placing that file."
            )
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {path}: {exc}") from exc
        rows = payload.get("capabilities") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CommandError(f'{path}: expected an object with a "capabilities" list.')

        plan, problems = self._plan(rows)
        if problems.lines:
            listing = "\n".join(f"  - {line}" for line in problems.lines)
            raise CommandError(
                f"{path}: {len(problems.lines)} problem(s), nothing was written:\n{listing}"
            )

        if options["dry_run"]:
            links = sum(len(item["links"]) for item in plan)
            self.stdout.write(
                f"[dry-run] {len(plan)} capabilities · {links} goal links — file is valid, nothing written."
            )
            return

        created_caps = kept_caps = created_links = kept_links = 0
        with transaction.atomic():
            for item in plan:
                capability, created = ProcedureCapability.objects.get_or_create(
                    template=item["template"], key=item["key"], defaults=item["fields"],
                )
                created_caps += created
                kept_caps += not created
                for link in item["links"]:
                    _, link_created = CapabilityGoalLink.objects.get_or_create(
                        capability=capability, goal=link["goal"], defaults=link["fields"],
                    )
                    created_links += link_created
                    kept_links += not link_created
        self.stdout.write(
            self.style.SUCCESS(
                f"Procedure knowledge seeded: +{created_caps} capabilities, +{created_links} goal links; "
                f"existing left as is: {kept_caps} capabilities, {kept_links} goal links. "
                f"Totals: capabilities={ProcedureCapability.objects.count()}, "
                f"goal links={CapabilityGoalLink.objects.count()}."
            )
        )

    def _plan(self, rows: list[Any]) -> tuple[list[dict[str, Any]], _Problems]:
        problems = _Problems()
        codes = {
            row.get("template_code") for row in rows
            if isinstance(row, dict) and isinstance(row.get("template_code"), str)
        }
        templates = {
            t.canonical_code: t for t in ServiceTemplate.objects.filter(canonical_code__in=codes)
        }
        goals = {g.key: g for g in GoalOption.objects.all()}

        plan: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, row in enumerate(rows, start=1):
            where = f"capabilities[{index}]"
            if not isinstance(row, dict):
                problems.add(where, "ожидался объект")
                continue
            code = _text(row, "template_code")
            key = _text(row, "key")
            where = f"capabilities[{index}] {code or '?'}:{key or '?'}"
            template = templates.get(code)
            if not code:
                problems.add(where, "template_code — не указан код шаблона процедуры")
            elif template is None:
                problems.add(
                    where,
                    f"template_code «{code}» не найден среди шаблонов "
                    "(сначала seed_canonical_catalog; код — ServiceTemplate.canonical_code)",
                )
            if not key:
                problems.add(where, "key — не указан ключ смысла")
            elif (code, key) in seen:
                problems.add(where, "такая пара (template_code, key) в файле уже есть")
            seen.add((code, key))

            fields = _claim_fields(row, where, problems)
            fields.update({name: _text(row, name) for name in _CAPABILITY_TEXT})

            links: list[dict[str, Any]] = []
            raw_links = row.get("goal_links") or []
            if not isinstance(raw_links, list):
                problems.add(where, "goal_links — ожидался список")
                raw_links = []
            seen_goals: set[str] = set()
            for link_index, raw in enumerate(raw_links, start=1):
                link_where = f"{where} goal_links[{link_index}]"
                if not isinstance(raw, dict):
                    problems.add(link_where, "ожидался объект")
                    continue
                goal_key = _text(raw, "goal")
                goal = goals.get(goal_key)
                if goal is None:
                    problems.add(
                        link_where,
                        f"goal «{goal_key}» не найдена среди целей "
                        "(сначала seed_goal_options; ключ — GoalOption.key)",
                    )
                if goal_key in seen_goals:
                    problems.add(link_where, "связь с этой целью у возможности в файле уже есть")
                seen_goals.add(goal_key)
                link_fields = _claim_fields(raw, link_where, problems)
                link_fields.update({name: _text(raw, name) for name in _LINK_TEXT})
                for field, message in course_errors(
                    course_pattern=link_fields["course_pattern"],
                    variability_note=link_fields["variability_note"],
                    evidence_source=link_fields["evidence_source"],
                    source_ref=link_fields["source_ref"],
                ).items():
                    problems.add(link_where, f"{field} — {message}")
                links.append({"goal": goal, "fields": link_fields})

            plan.append({"template": template, "key": key, "fields": fields, "links": links})
        return plan, problems
