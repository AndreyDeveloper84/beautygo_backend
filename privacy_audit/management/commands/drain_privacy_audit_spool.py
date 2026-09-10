"""Move queued access records into the journal.

Owner §107 allows a guaranteed queue for operations that neither disclose nor
destroy. This command is the half that makes «гарантированная» true: without
something that drains it, a queue is a place records go to be forgotten
quietly.

Run it on a schedule, and — this is the part ops has to know — **run it where
the spool file is**. The queue is deliberately a local file rather than a
table, because a queue living in the same database as the journal fails for
the same reasons the journal does and insures nothing (executor rules §23: a
guard inside the thing it watches). The cost of that choice is locality: on a
multi-instance deployment each instance holds its own spool, so either the
command runs on every instance or ``PRIVACY_AUDIT_SPOOL_DIR`` points at shared
storage.

``--check`` reports the backlog without writing anything, for a monitor. It
exits non-zero when anything is waiting, so "the queue is empty" is an
assertion something makes rather than a silence somebody interprets.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from privacy_audit import spool


class Command(BaseCommand):
    help = "Записать в журнал доступа записи, стоящие в гарантированной очереди."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--check", action="store_true",
            help=(
                "Только показать размер очереди и выйти. Ненулевой код "
                "выхода, если очередь непуста."
            ),
        )

    def handle(self, *args, **options) -> None:
        directory = spool.spool_dir()
        pending = spool.pending_count()

        if options["check"]:
            self.stdout.write(f"spool_dir={directory}")
            self.stdout.write(f"pending={pending}")
            if pending:
                # Non-zero on a backlog: a monitor must be able to fail on
                # this without parsing text.
                raise SystemExit(1)
            return

        written, failed = spool.drain()
        remaining = spool.pending_count()
        self.stdout.write(f"spool_dir={directory}")
        self.stdout.write(
            f"pending_before={pending} written={written} failed={failed} "
            f"remaining={remaining}"
        )
        # The subject printed next to the result: how many were waiting, how
        # many landed, how many are still there. "Command succeeded" and "the
        # queue is empty" are independent facts.
        if failed:
            raise SystemExit(1)
