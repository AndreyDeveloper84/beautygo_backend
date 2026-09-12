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

from services.mapping import ApplyNotAuthorized, RulesEnabled, authorize_apply, resolve_tenant
from services.mapping.report import row_lines, summary_lines, write_json
from services.mapping.schema import SchemaNotReady, describe_subject
from services.mapping.store import store_report
from services.mapping.types import RULE_VERSION
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
        parser.add_argument("--apply", action="store_true", help="отказывает с названной причиной (OD-NEW-7)")
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

        if options["apply"]:
            try:
                authorize_apply(tenant_slug=tenant.slug)
            except ApplyNotAuthorized as exc:
                raise CommandError(str(exc))

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
