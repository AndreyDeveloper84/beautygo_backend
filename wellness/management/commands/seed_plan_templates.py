"""Положить таблицу шаблонов Plan Lite §51 (DRF-2123).

Идемпотентно: повтор → ``created=0``. Изменился текст/действия строки в
``wellness/plan_lite_templates.py`` → новая версия, прежняя активная
снимается (``is_active=False``), ничего не удаляется. Правки владельца в
админке живут до следующего запуска сида по той же цели: сид кладёт свою
версию поверх, прежняя остаётся историей.

Usage::

    python manage.py seed_plan_templates
    python manage.py seed_plan_templates --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from wellness import plan_lite_templates


class Command(BaseCommand):
    help = "Seed wellness.PlanTemplate from the owner's table (§51); idempotent, versioned."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options) -> None:
        rows = plan_lite_templates.PLAN_TEMPLATES_SEED
        if options["dry_run"]:
            with transaction.atomic():
                report = plan_lite_templates.seed_plan_templates(rows)
                transaction.set_rollback(True)
            prefix = "[dry-run] "
        else:
            report = plan_lite_templates.seed_plan_templates(rows)
            prefix = ""
        self.stdout.write(
            f"{prefix}seed_plan_templates: rows={len(rows)} "
            f"created={report.created} deactivated={report.deactivated} unchanged={report.unchanged}"
        )
