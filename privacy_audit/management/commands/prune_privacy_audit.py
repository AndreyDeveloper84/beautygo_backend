"""``prune_privacy_audit`` — ретенция журнала доступа к персданным (DRF-1782, §96).

Единственный путь удаления строк журнала: ``privacy_audit.prune_expired``.
По умолчанию — сухой прогон: печатает, сколько строк старше периода,
ничего не удаляя. ``--apply`` удаляет. Рядом с результатом печатается
предмет (хост, контейнер, БД — ``core.measurement_subject``) и версия кода:
число без предмета не о том.

Расписание (beat) ставится после первой ручной прогонки на пилоте — не здесь.
"""

from __future__ import annotations

import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from privacy_audit.retention import (
    RETENTION_SETTING,
    RetentionMisconfigured,
    prune_expired,
    retention_days,
)


def _code_version() -> str:
    version = str(getattr(settings, "APP_VERSION", "") or "")
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return " ".join(x for x in (version, sha) if x) or "—"


class Command(BaseCommand):
    help = "Удалить строки журнала доступа к персданным старше PRIVACY_AUDIT_RETENTION_DAYS (§96)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Удалить. Без флага — сухой прогон: только посчитать.",
        )

    def handle(self, *args, **options):
        from core.measurement_subject import subject_lines

        try:
            days = retention_days()
        except RetentionMisconfigured as exc:
            raise CommandError(f"{exc}; ничего не удалено") from exc

        dry_run = not options["apply"]
        outcome = prune_expired(dry_run=dry_run)

        for line in subject_lines():
            self.stdout.write(line)
        self.stdout.write(f"код                        : {_code_version()}")
        self.stdout.write("== РЕТЕНЦИЯ privacy_audit ==")
        self.stdout.write(f"период ({RETENTION_SETTING}) : {days} дн — временное решение §96")
        self.stdout.write(f"граница (occurred_at <)    : {outcome.cutoff.isoformat()}")
        self.stdout.write(f"строк старше границы       : {outcome.matched}")
        self.stdout.write(
            f"удалено                    : {outcome.deleted}"
            + ("  (сухой прогон, --apply не задан)" if dry_run else "")
        )
        self.stdout.write(f"осталось строк             : {outcome.remaining}")
        oldest = outcome.oldest_remaining.isoformat() if outcome.oldest_remaining else "—"
        self.stdout.write(f"самая старая оставшаяся    : {oldest}")
