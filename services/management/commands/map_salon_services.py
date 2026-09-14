"""``manage.py map_salon_services --tenant <slug> [--rules R1,R2] [--out <path>]`` — Level A, dry-run.

Печатает предмет (база, host, старт постмастера, число канонов с кодом),
потом по строке на каждую услугу салона: решение, причина, флаги,
кандидаты с evidence, что стоит сейчас, план провенанса (для
``AUTO_ELIGIBLE``), и сводку, посчитанную из тех же строк.

**Ничего не пишет.** ``--apply`` принимается только чтобы ответить отказом
с названной причиной (``authorize_apply``): OD-NEW-7 не принят, запись —
MAP-AUTO-06. Правила по умолчанию выключены — без ``--rules`` каждый
кандидат-под-правило показывается как ``AUTO_NOT_ENABLED``.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from services.canonical_code import SEED_PATH
from services.mapping import ApplyNotAuthorized, RulesEnabled, resolve_tenant
from services.mapping.report import row_lines, summary_lines, write_json
from services.mapping.schema import SchemaNotReady, describe_subject
from services.mapping.store import store_report
from services.mapping.types import RULE_VERSION
from services.mapping_apply import ApplyStopped, apply_tenant, rollback_auto_rule
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Level A резолвер услуг салона → канон. Только dry-run; --apply отказывает с причиной."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="slug салона")
        parser.add_argument(
            "--rules", default="", help="включённые правила через запятую: R1,R2 (по умолчанию — ни одно)",
        )
        parser.add_argument("--out", default=None, help="куда записать JSON-отчёт (файл, не база)")
        parser.add_argument(
            "--dry-run", action="store_true", default=True, help="единственный режим; принимается для явности",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="записать AUTO_ELIGIBLE через форму §76 (MAP-AUTO-06); сегодня ворота закрыты — отказ OD-NEW-7",
        )
        parser.add_argument(
            "--rollback", action="store_true",
            help="снять записи правила данной --rule-version у салона (владельца не трогает); те же ворота",
        )
        parser.add_argument("--rule-version", default=RULE_VERSION, help="версия правил, на которой посчитан план")
        parser.add_argument("--seed-version", default=SEED_PATH.name, help="файл seed, на котором посчитан план")
        parser.add_argument("--only", default="", help="uuid услуг через запятую — применить только их")
        parser.add_argument("--per-row", action="store_true", help="каждая строка своей транзакцией")
        parser.add_argument(
            "--store", action="store_true",
            help=("сохранить отчёт как «последний прогон» салона "
                  "(файл MAPPING_REPORT_DIR/<slug>.json) — его читает админка"),
        )

    def handle(self, *args, **options):
        try:
            tenant = Tenant.objects.get(slug=options["tenant"])
        except Tenant.DoesNotExist:
            raise CommandError(f"салон «{options['tenant']}» не найден")
        try:
            rules = RulesEnabled.parse(options["rules"])
        except ValueError as exc:
            raise CommandError(str(exc))

        if options["apply"] or options["rollback"]:
            return self._write(tenant, rules, options)

        report_ref = options["out"] or "dry-run"
        try:
            rows = resolve_tenant(tenant, rules, report_ref=report_ref)
        except SchemaNotReady as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            raise SystemExit(2)

        w = self.stdout.write
        w(describe_subject().line())
        w(f"салон: {tenant.slug} ({tenant.pk}) · правила: "
          f"{', '.join(r for r in ('R0', 'R1', 'R2') if getattr(rules, r)) or 'ни одно (AUTO_NOT_ENABLED)'} "
          f"· версия правил: {RULE_VERSION} · режим: dry-run, записи нет")
        w("")
        for r in rows:
            for line in row_lines(r):
                w(line)
        w("")
        for line in summary_lines(rows):
            w(line)
        if options["out"]:
            write_json(rows, Path(options["out"]))
            w(f"JSON: {options['out']}")
        if options["store"]:
            path = store_report(rows, tenant.slug, rules=options["rules"] or "")
            w(f"отчёт последнего прогона (для админки): {path}")

    def _write(self, tenant, rules, options) -> None:
        """Apply / откат. Ворота — внутри (authorize_apply); сегодня всегда отказ."""
        w = self.stdout.write
        try:
            if options["rollback"]:
                rep = rollback_auto_rule(tenant, rule_version=options["rule_version"])
                w(f"откат {tenant.slug} v={options['rule_version']}: снято строк {len(rep.reverted)}, "
                  f"синонимов {rep.synonyms_removed}")
                return
            only = {s.strip() for s in options["only"].split(",") if s.strip()} or None
            rep = apply_tenant(
                tenant, rules, rule_version=options["rule_version"], seed_version=options["seed_version"],
                report_ref=options["out"] or "apply", only=only, per_row=options["per_row"],
            )
        except ApplyNotAuthorized as exc:
            raise CommandError(str(exc))
        except (ApplyStopped, SchemaNotReady) as exc:
            raise CommandError(f"apply остановлен, записей нет: {exc}")
        w(describe_subject().line())
        w(f"apply {tenant.slug}: план {rep.planned}, к записи {rep.eligible}, записано {rep.written}, "
          f"пропущено {rep.skipped}, провалено {rep.failed}")
        for r in rep.rows:
            w(f"  {r.outcome:<8} {r.name} — {r.detail}")
        w(f"рёбра мастер×услуга: до {dict(rep.edges_verdict_before)} → после {dict(rep.edges_verdict_after)}")
