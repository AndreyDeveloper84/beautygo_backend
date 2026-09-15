"""Correct a completed visit to no-show — the operator path (DRF-1852, OD-V2).

Owner decision OD-V2 (Linear DRF-1064, 2026-08-15): a visit closed as
completed may be corrected to no-show within a day, by support, with a
record of who corrected it and why. No product flow, no salon
self-service, no Django admin (it bypasses the state machine).

    manage.py correct_completion_to_no_show <appointment_id> \
        --reason "клиент не пришёл, салон сообщил в поддержку" \
        --operator "support: Ivan" --dry-run

``--reason`` and ``--operator`` are required; there is no silent mode.
Payments are not touched — a captured fee is returned by hand.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from appointments.application.services.completion import (
    CorrectionRefused,
    correct_completion_to_no_show,
    correction_window,
)
from appointments.models import Appointment


class Command(BaseCommand):
    help = (
        "Correct a completed visit to no-show within "
        "BOOKING_COMPLETION_CORRECTION_WINDOW_HOURS (owner decision OD-V2)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("appointment_id", help="Appointment UUID.")
        parser.add_argument(
            "--reason", required=True,
            help="Why the visit is being corrected. Required.",
        )
        parser.add_argument(
            "--operator", required=True,
            help="Who is correcting it (support operator). Required.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would happen and exit without writing.",
        )

    def handle(self, *args, **options) -> None:
        appointment_id = options["appointment_id"]
        window = correction_window()
        appt = Appointment.objects.filter(pk=appointment_id).first()
        if appt is None:
            raise CommandError(f"not_found: no appointment {appointment_id}")

        age = (timezone.now() - appt.completed_at) if appt.completed_at else None
        self.stdout.write(
            f"{appt.id}: status={appt.status} completed_at={appt.completed_at} "
            f"completed_by={appt.completed_by or '-'} age={age} window={window}"
        )

        if options["dry_run"]:
            verdict = "would correct to no_show"
            if appt.status != Appointment.Status.COMPLETED:
                verdict = "would refuse: not_completed"
            elif age is None:
                verdict = "would refuse: no_completed_at"
            elif age > window:
                verdict = "would refuse: window_closed"
            self.stdout.write(self.style.WARNING(f"dry-run: {verdict}; nothing written"))
            return

        try:
            correct_completion_to_no_show(
                appt.id, reason=options["reason"], operator=options["operator"],
            )
        except CorrectionRefused as refused:
            raise CommandError(str(refused)) from None

        self.stdout.write(self.style.SUCCESS(
            f"corrected {appt.id}: completed -> no_show "
            f"(operator={options['operator'].strip()})"
        ))
        self.stdout.write(self.style.WARNING(
            "Payments are not touched: if the platform fee was captured, "
            "return it by hand."
        ))
