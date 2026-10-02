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
  говорится).
* **Импорт — не подтверждение.** ``approved`` из файла принимается только
  правилом из :data:`services.knowledge_intake.KNOWN_CONFIRMATION_RULES`, а
  этот набор сегодня пуст — то есть из файла заводится только черновик, и
  подтверждает его человек в админке. Строка ``approved`` с любым другим
  ``confirmed_rule`` — ошибка файла, а не черновик: тихое понижение спрятало
  бы от куратора, что его «подтверждено» не принято.
* Сначала проверяет файл целиком, потом пишет. Любая ошибка — неизвестный код
  шаблона, неизвестная цель, неизвестное поле, значение не того типа, слишком
  длинный текст, подтверждение без основания, курс или срок результата голым
  числом либо без оговорки и источника, запрет без предмета — печатается
  списком, и не пишется ничего.

Чего команда не делает
----------------------
* Знание не выдумывает и из названий услуг не выводит.
* Новое издание файла на уже заведённые строки не ложится (см. выше) — правка
  существующего делается в админке.
* **Удалённое в админке возвращает.** Строка, которая есть в файле, при
  следующем запуске заводится заново: команда не знает, что её удалили
  намеренно. Чтобы убрать строку, пришедшую из файла, её убирают из файла —
  или оставляют в базе не подтверждённой.
* Шаблоны и цели не заводит: им нужны ``seed_canonical_catalog`` и
  ``seed_goal_options``, и порядок именно такой.
* Шаблон адресуется кодом эталонного справочника (``canonical_code``), и
  только им. Код ставит не засев каталога, а начальное присвоение
  (:func:`services.canonical_code.bootstrap_canonical_codes`, миграция 0024) —
  тем шаблонам, что были в базе на тот момент. Шаблон без кода из файла не
  адресуется: знание о нём вносится в админке.

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
        "expected_effect": "...",
        "result_timeframe": "...",         # словами, не числом; только вместе с
        "variability_note": "...",         # оговоркой, источником и ссылкой
        "claim_scope": "supported",        # | not_supported | prohibited_claim
        "prohibited_statement": "",        # только у prohibited_claim и там обязателен:
                                           # что именно нельзя утверждать
        "limitations": "...", "evidence_source": "...",
        "evidence_kind": "professional_consensus",  # пусто либо из закрытого списка:
        #   clinical_guideline | systematic_review | rct | manufacturer_ifu |
        #   regulatory_document | professional_consensus | legal_rule | product_policy
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

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from services.knowledge_intake import (
    APPROVED,
    course_errors,
    evidence_kind_errors,
    prohibition_errors,
    provenance_errors,
    rule_problem,
    timeframe_errors,
)
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

_CAPABILITY_TEXT = (
    "text_client", "text_professional", "expected_effect", "result_timeframe", "variability_note",
)
_LINK_TEXT = ("course_pattern", "result_horizon", "variability_note")
_CLAIM_TEXT = (
    "limitations", "prohibited_statement", "evidence_source", "evidence_kind", "source_ref",
    "confirmed_rule", "rule_version",
)
_CLAIM_KEYS = frozenset({"status", "claim_scope", "confirmed_at", "valid_until", *_CLAIM_TEXT})
_CAPABILITY_KEYS = _CLAIM_KEYS | {"template_code", "key", "goal_links", *_CAPABILITY_TEXT}
_LINK_KEYS = _CLAIM_KEYS | {"goal", *_LINK_TEXT}

#: Эти поля проверены здесь словами для человека; модель их повторно не судит.
_OWN_CHECKS = {"status", "claim_scope", "confirmed_by", "evidence_kind"}


class _Problems:
    """Все ошибки файла сразу — куратор правит файл за один проход, не по одной."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.lines.append(f"{where}: {message}")


def _text(row: dict[str, Any], name: str, where: str, problems: _Problems) -> str:
    """Строковое поле файла. Не строка — ошибка, а не молчаливая пустота."""
    value = row.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        problems.add(where, f"{name} — ожидалась строка, получено {type(value).__name__}")
        return ""
    return value.strip()


def _unknown_keys(row: dict[str, Any], allowed: frozenset[str], where: str, problems: _Problems) -> None:
    for name in sorted(set(row) - allowed):
        problems.add(where, f"неизвестное поле «{name}» (опечатка? допустимо: {sorted(allowed)})")


def _moment(row: dict[str, Any], name: str, where: str, problems: _Problems) -> Any:
    raw = row.get(name)
    if raw in (None, ""):
        return None
    parsed = None
    if isinstance(raw, str):
        try:
            parsed = parse_datetime(raw)
        except ValueError:  # формат верный, а даты такой нет («30 февраля»)
            parsed = None
    if parsed is None or parsed.tzinfo is None:
        problems.add(
            where, f"{name} — нужна существующая дата и время с часовым поясом (ISO 8601), получено «{raw}»",
        )
        return None
    return parsed


def _claim_fields(
    row: dict[str, Any], where: str, problems: _Problems, *, has_client_text: bool,
) -> dict[str, Any]:
    """Общие поля основания одной строки файла — проверенные, готовые к записи.

    ``has_client_text`` — есть ли у строки формулировка для клиента: у
    возможности есть, у связи с целью такого поля нет.
    """
    status = _text(row, "status", where, problems) or ClaimEvidence.Status.SYSTEM_INFERENCE.value
    scope = _text(row, "claim_scope", where, problems) or ClaimEvidence.ClaimScope.NOT_SUPPORTED.value
    if status not in _STATUSES:
        problems.add(where, f"неизвестный status «{status}» (допустимо: {sorted(_STATUSES)})")
    if scope not in _SCOPES:
        problems.add(where, f"неизвестный claim_scope «{scope}» (допустимо: {sorted(_SCOPES)})")

    fields: dict[str, Any] = {name: _text(row, name, where, problems) for name in _CLAIM_TEXT}
    fields["status"] = status
    fields["claim_scope"] = scope
    fields["confirmed_at"] = _moment(row, "confirmed_at", where, problems)
    fields["valid_until"] = _moment(row, "valid_until", where, problems)

    for field, message in evidence_kind_errors(
        status=status, evidence_kind=fields["evidence_kind"],
    ).items():
        problems.add(where, f"{field} — {message}")

    if status == APPROVED:
        # Импорт — не подтверждение: правило должно быть известным, а не любым.
        unknown_rule = rule_problem(fields["confirmed_rule"], fields["rule_version"])
        if unknown_rule:
            problems.add(where, f"status approved — {unknown_rule}")
        for field, message in provenance_errors(
            status=status, source_ref=fields["source_ref"], has_confirmer=True,
        ).items():
            problems.add(where, f"{field} — {message}")
        if fields["confirmed_at"] is None:
            problems.add(where, "confirmed_at — у подтверждённой строки должна быть дата подтверждения.")
    else:
        # Отметки подтверждения у черновика были бы утверждением, которого
        # никто не делал; из файла они в базу не едут.
        fields["confirmed_rule"] = ""
        fields["rule_version"] = ""
        fields["confirmed_at"] = None

    # Тип ``text_client`` здесь не судится (это делает разбор возможности) —
    # отсюда одноразовый приёмник ошибок.
    for field, message in prohibition_errors(
        claim_scope=scope,
        prohibited_statement=fields["prohibited_statement"],
        text_client=_text(row, "text_client", where, _Problems()) if has_client_text else None,
    ).items():
        problems.add(where, f"{field} — {message}")
    return fields


def _model_problems(instance: Any, exclude: set[str], where: str, problems: _Problems) -> None:
    """Длины, slug и прочее, что знает сама модель, — до записи, а не из базы."""
    try:
        instance.clean_fields(exclude=exclude | _OWN_CHECKS)
    except ValidationError as exc:
        for field, messages in exc.message_dict.items():
            problems.add(where, f"{field} — {' '.join(messages)}")


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
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise CommandError(f"{path} is not UTF-8: {exc}") from exc
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
            row["template_code"].strip() for row in rows
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
            code = _text(row, "template_code", where, problems)
            key = _text(row, "key", where, problems)
            where = f"capabilities[{index}] {code or '?'}:{key or '?'}"
            _unknown_keys(row, _CAPABILITY_KEYS, where, problems)
            template = templates.get(code)
            if not code:
                problems.add(where, "template_code — не указан код шаблона процедуры")
            elif template is None:
                problems.add(
                    where,
                    f"template_code «{code}» не найден среди шаблонов "
                    "(код — ServiceTemplate.canonical_code; его нет у шаблонов, заведённых "
                    "после начального присвоения кодов, и у черновых канонов)",
                )
            if not key:
                problems.add(where, "key — не указан ключ смысла")
            elif (code, key) in seen:
                problems.add(where, "такая пара (template_code, key) в файле уже есть")
            seen.add((code, key))

            fields = _claim_fields(row, where, problems, has_client_text=True)
            fields.update({name: _text(row, name, where, problems) for name in _CAPABILITY_TEXT})
            for field, message in timeframe_errors(
                field="result_timeframe",
                value=fields["result_timeframe"],
                variability_note=fields["variability_note"],
                evidence_source=fields["evidence_source"],
                source_ref=fields["source_ref"],
            ).items():
                problems.add(where, f"{field} — {message}")
            if key:
                _model_problems(ProcedureCapability(key=key, **fields), {"template"}, where, problems)

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
                _unknown_keys(raw, _LINK_KEYS, link_where, problems)
                goal_key = _text(raw, "goal", link_where, problems)
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
                link_fields = _claim_fields(raw, link_where, problems, has_client_text=False)
                link_fields.update(
                    {name: _text(raw, name, link_where, problems) for name in _LINK_TEXT}
                )
                for field, message in course_errors(
                    course_pattern=link_fields["course_pattern"],
                    variability_note=link_fields["variability_note"],
                    evidence_source=link_fields["evidence_source"],
                    source_ref=link_fields["source_ref"],
                ).items():
                    problems.add(link_where, f"{field} — {message}")
                for field, message in timeframe_errors(
                    field="result_horizon",
                    value=link_fields["result_horizon"],
                    variability_note=link_fields["variability_note"],
                    evidence_source=link_fields["evidence_source"],
                    source_ref=link_fields["source_ref"],
                ).items():
                    problems.add(link_where, f"{field} — {message}")
                _model_problems(
                    CapabilityGoalLink(**link_fields), {"capability", "goal"}, link_where, problems,
                )
                links.append({"goal": goal, "fields": link_fields})

            plan.append({"template": template, "key": key, "fields": fields, "links": links})
        return plan, problems
