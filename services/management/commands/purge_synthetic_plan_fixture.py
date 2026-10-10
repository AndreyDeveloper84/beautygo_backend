"""Убрать синтетический тестовый набор Плана — обратный ход засева.

    manage.py purge_synthetic_plan_fixture [--spec ФАЙЛ] (--dry-run | --apply)

Режим называется явно: без ``--dry-run`` или ``--apply`` команда ничего не
делает и отказывает. ``--dry-run`` исполняет всё в транзакции и откатывает
её: в базе ничего не меняется, а вывод тот же, что был бы при ``--apply``.

Что удаляется, что остаётся и почему — ``_synthetic_plan_purge`` рядом.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ._synthetic_plan_fixture import DEFAULT_SPEC, FixtureRefused, load_spec
from ._synthetic_plan_purge import purge

OUTSIDE_THE_CATALOG = (
    "строка тест-мастера в зеркале каталога у бота",
    "журналы и уже отправленные события о тестовом прогоне",
    "настройки стенда: разрешение тестовой персоне (SYNTHETIC_TEST_DATA_ENABLED, SYNTHETIC_TEST_SUBJECT_IDS)",
)


class Command(BaseCommand):
    help = "Убрать синтетический тестовый набор Плана (только помеченные строки набора)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--spec", default=str(DEFAULT_SPEC), help="Файл данных набора (JSON).")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Показать, что было бы сделано; ничего не менять.")
        mode.add_argument("--apply", action="store_true", help="Исполнить очистку.")

    def handle(self, *args, **options) -> None:
        dry_run, apply = options["dry_run"], options["apply"]
        if not dry_run and not apply:
            raise CommandError("Режим не назван: --dry-run (показать) или --apply (исполнить). Ничего не сделано.")
        try:
            report = purge(load_spec(options["spec"]), dry_run=dry_run)
        except FixtureRefused as refusal:
            raise CommandError(f"Очистка НЕ выполнена, в базе ничего не изменено. Причина — {refusal}") from refusal

        write = self.stdout.write
        write("ПРОБНЫЙ ПРОГОН — в базе ничего не изменено." if dry_run else "Очистка выполнена.")
        self._section(f"{'Было бы удалено' if dry_run else 'Удалено'}", "-", report.removed)
        self._section(
            "Вместе с ними каскадом", "-", [f"{label} — {count}" for label, count in sorted(report.cascaded.items())],
        )
        self._section(f"{'Осталось бы' if dry_run else 'Осталось'} в базе", "!", report.kept)
        self._section("Выведено из продажи вместо удаления", "!", report.disabled)
        self._section("В базе не найдено", "=", report.absent)
        self._section(
            "Следы прогона — команда их НЕ удаляет", "!",
            [f"{what} — {count}" for what, count in sorted(report.run_traces.items())],
        )
        self._section("Вне каталога — команда этого не касается", "·", list(OUTSIDE_THE_CATALOG))

    def _section(self, title: str, mark: str, lines: list[str]) -> None:
        self.stdout.write(f"{title} ({len(lines)}):")
        for line in lines:
            self.stdout.write(f"  {mark} {line}")
