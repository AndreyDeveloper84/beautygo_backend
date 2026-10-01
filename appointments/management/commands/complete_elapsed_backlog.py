"""Close the historical tail of never-closed visits (DRF-1064, block B).

The 15-minute sweep refuses to reach back past
``BOOKING_AUTO_COMPLETE_NOT_BEFORE`` — see that setting for why. This
command is the other half of that decision: the way the backlog gets
drained, deliberately, by a person who knows what it costs.

It costs real things. Every closure charges the platform fee and asks
the client for a review, so draining months of forgotten bookings in one
run means a burst of both — to people whose visit was in the spring.
Hence: ``--dry-run`` first, an explicit window, and a batch cap.

    manage.py complete_elapsed_backlog --since 2026-08-01 --dry-run
    manage.py complete_elapsed_backlog --since 2026-08-01 --limit 50 \\
        --operator ops --database <name>

``--since`` is required. There is no "everything" mode on purpose.

The write names its database and leaves a receipt (DRF-2689). A closure
charges the platform fee, so a run that writes requires ``--database`` and
refuses, before the first closure, unless it names the database this
process is connected to; ``--operator`` says who ran it. Every run, dry or
real, leaves one ``complete_elapsed_backlog_run`` receipt — on a real run
written BEFORE the first closure (no receipt, nothing closed), then
completed with the counters. ``--dry-run`` stays what it was: opt-in, and
needs neither key. See ``payments/money_commands.py`` for the shape.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from appointments.models import Appointment
from appointments.tasks import complete_elapsed_bookings
from payments.money_commands import (
    ReceiptUnwritten,
    clean_operator,
    provider_mode,
    require_named_database,
    write_receipt,
)

logger = logging.getLogger(__name__)

COMMAND = "complete_elapsed_backlog"


class Command(BaseCommand):
    help = (
        "Complete confirmed bookings whose time elapsed, from an explicit "
        "start date. The periodic sweep never touches this backlog."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--since", required=True,
            help=(
                "Earliest end_datetime to touch: YYYY-MM-DD or a full "
                "ISO-8601 instant. Required — there is no 'all history' mode."
            ),
        )
        parser.add_argument(
            "--hours", type=int, default=None,
            help=(
                "Grace period after end_datetime. Defaults to "
                "BOOKING_AUTO_COMPLETE_AFTER_HOURS."
            ),
        )
        parser.add_argument(
            "--limit", type=int, default=100,
            help="Maximum bookings to close in this run (default 100).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would be closed and exit without writing.",
        )
        parser.add_argument(
            "--database", type=str, default=None,
            help=(
                "Name of the database you mean to close visits in. Required "
                "to write; must be the one this process is connected to."
            ),
        )
        parser.add_argument(
            "--operator", type=str, default=None,
            help=(
                "Who runs it — a role or a tag, not a name; goes into the "
                "run receipt. Required to write."
            ),
        )

    def handle(self, *args, **options) -> None:
        writing = not options["dry_run"]
        operator = clean_operator(
            options["operator"], command=COMMAND, required=writing,
        )
        database = require_named_database(
            options["database"], command=COMMAND, required=writing,
        )

        raw = options["since"].strip()
        not_before = parse_datetime(raw)
        if not_before is None:
            as_date = parse_date(raw)
            if as_date is None:
                raise CommandError(
                    f"--since {raw!r} is neither a date (YYYY-MM-DD) nor an "
                    "ISO-8601 instant."
                )
            not_before = timezone.make_aware(
                datetime.combine(as_date, time.min),
            )
        elif timezone.is_naive(not_before):
            not_before = timezone.make_aware(not_before)

        hours = options["hours"]
        if hours is None:
            hours = getattr(settings, "BOOKING_AUTO_COMPLETE_AFTER_HOURS", 3)
        cutoff = timezone.now() - timedelta(hours=hours)

        if cutoff <= not_before:
            raise CommandError(
                f"--since is later than the {hours}h cutoff "
                f"({cutoff:%Y-%m-%d %H:%M}) — nothing in that window has "
                "elapsed yet."
            )

        candidates = (
            Appointment.objects
            .filter(
                status=Appointment.Status.CONFIRMED,
                end_datetime__lte=cutoff,
                end_datetime__gte=not_before,
            )
            .order_by("end_datetime")
        )
        total = candidates.count()
        limit = options["limit"]

        self.stdout.write(
            f"{total} confirmed booking(s) ended between "
            f"{not_before:%Y-%m-%d %H:%M} and {cutoff:%Y-%m-%d %H:%M}."
        )
        if total > limit:
            self.stdout.write(self.style.WARNING(
                f"Capped at --limit {limit}; re-run to continue."
            ))

        receipt = {
            "operator": operator or None,
            "mode": "apply" if writing else "dry_run",
            "database": database,
            "provider_mode": provider_mode(),
            "since": not_before.isoformat(),
            "cutoff": cutoff.isoformat(),
            "hours": hours,
            "limit": limit,
            "matched": total,
            "planned": min(total, limit),
            # Filled in after a real run; null on a dry run and on a run
            # that did not get as far as closing anything.
            "result": None,
        }

        if options["dry_run"]:
            # Nothing to lose on a dry run — its receipt is best-effort.
            try:
                write_receipt(event_catalogue.COMPLETE_ELAPSED_BACKLOG_RUN, receipt)
            except Exception:  # noqa: BLE001 — a dry run is not failed by its receipt
                logger.exception("appointments.backlog.receipt_write_failed")
                self.stderr.write("dry-run receipt not written (see the log)")
            for appt in candidates[:limit]:
                self.stdout.write(
                    f"  would close {appt.id} — ended "
                    f"{appt.end_datetime:%Y-%m-%d %H:%M} — "
                    f"{appt.snapshot_service_name or 'service'}"
                )
            self.stdout.write(self.style.WARNING("dry-run: nothing written"))
            return

        # Fail-closed, and BEFORE the first closure: a closure charges the
        # fee through its own transactions, which a receipt that failed
        # afterwards could not take back.
        try:
            row = write_receipt(event_catalogue.COMPLETE_ELAPSED_BACKLOG_RUN, receipt)
        except Exception as exc:  # noqa: BLE001 — any receipt failure is a refusal
            raise ReceiptUnwritten(
                "run receipt not written — NOTHING was closed "
                f"({type(exc).__name__})"
            ) from exc

        result = complete_elapsed_bookings(
            not_before=not_before, cutoff=cutoff, batch_size=limit,
        )
        # The counters complete the receipt. Best-effort: the closures are
        # done and visible in their rows; the intent is already recorded.
        try:
            AnalyticsEvent.objects.filter(pk=row.pk).update(payload={
                **receipt,
                "result": {
                    key: result[key] for key in ("completed", "skipped", "failed")
                },
            })
        except Exception:  # noqa: BLE001
            logger.exception("appointments.backlog.receipt_result_write_failed")
            self.stderr.write("receipt counters not written (see the log)")
        self.stdout.write(self.style.SUCCESS(
            f"completed={result['completed']} skipped={result['skipped']} "
            f"failed={result['failed']}"
        ))
        if result["failed"]:
            self.stdout.write(self.style.ERROR(
                "Some rows failed — see the log for appointment ids. "
                "Re-running is safe: closed rows are skipped."
            ))
