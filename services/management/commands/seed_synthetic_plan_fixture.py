"""Засеять синтетический тестовый набор для сквозной проверки Плана.

    manage.py seed_synthetic_plan_fixture [--spec ФАЙЛ] [--dry-run]

Что делает, чего не делает и почему — ``_synthetic_plan_fixture`` рядом.
``--dry-run`` исполняет всё в транзакции и откатывает её: в базе ничего не
остаётся, а вывод тот же, что был бы при настоящем запуске.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ._synthetic_plan_fixture import DEFAULT_SPEC, FixtureRefused, load_spec, seed


class Command(BaseCommand):
    help = "Засеять синтетический тестовый набор Плана (только демо-салон, только помеченные строки)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--spec", default=str(DEFAULT_SPEC), help="Файл данных набора (JSON).")
        parser.add_argument("--dry-run", action="store_true", help="Показать, что было бы сделано; ничего не писать.")

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        try:
            report = seed(load_spec(options["spec"]), dry_run=dry_run)
        except FixtureRefused as refusal:
            raise CommandError(f"Набор НЕ засеян, в базе ничего не изменено. Причина — {refusal}") from refusal

        write = self.stdout.write
        write("ПРОБНЫЙ ПРОГОН — в базе ничего не изменено." if dry_run else "Набор засеян.")
        write(f"Подтверждённых настоящих способностей у цели до засева: {report.real_capabilities_of_goal}")
        write(f"{'Было бы создано' if dry_run else 'Создано'} ({len(report.created)}):")
        for line in report.created:
            write(f"  + {line}")
        write(f"Уже было, совпадает с файлом ({len(report.found)}):")
        for line in report.found:
            write(f"  = {line}")
        write(f"Тест-мастер: {report.master_id}")
        write("Что видит обычный читатель знания у этой цели (без разрешения): "
              f"{report.visible_without_grant or 'ничего'}")
        if report.visible_under_grant is None:
            write(f"Под разрешением тестовой персоны: не проверено — {report.grant_note}")
        else:
            write(f"Под разрешением тестовой персоны: {report.visible_under_grant}")
