"""Manual capture re-run for stuck two-stage payments (D9, ADR §2).

Usage:
    python manage.py retry_capture                     # all stuck holds
    python manage.py retry_capture --payment-id <uuid> # one payment
    python manage.py retry_capture --sync              # inline, no broker

"Stuck" = authorized (held) payments whose capture is scheduled or has
failed and whose planned capture time has arrived (or was never set —
e.g. the schedule hook failed after complete()). The task itself is
idempotent (stable YooKassa key ``capture-{payment.id}`` + local state
guard), so running this command liberally is safe.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from payments.models import Payment
from payments.tasks import capture_payment_task


class Command(BaseCommand):
    help = "Re-run capture for stuck held payments (waiting_for_capture)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--payment-id', type=str, default=None,
            help='Retry capture for a single Payment UUID.',
        )
        parser.add_argument(
            '--sync', action='store_true',
            help='Run the capture task inline instead of enqueueing.',
        )

    def handle(self, *args, **options):
        qs = Payment.objects.filter(
            status=Payment.Status.AUTHORIZED,
            capture_state__in=[
                Payment.CaptureState.SCHEDULED,
                Payment.CaptureState.CAPTURE_FAILED,
            ],
        )
        if options['payment_id']:
            qs = qs.filter(pk=options['payment_id'])
            if not qs.exists():
                raise CommandError(
                    'No stuck held payment with id '
                    f'{options["payment_id"]} (must be authorized with '
                    'capture_state scheduled/capture_failed).',
                )
        else:
            now = timezone.now()
            qs = qs.filter(capture_scheduled_for__isnull=True) | qs.filter(
                capture_scheduled_for__lte=now,
            )

        count = 0
        for payment in qs.iterator():
            if options['sync']:
                capture_payment_task(str(payment.id))
            else:
                capture_payment_task.apply_async(args=[str(payment.id)])
            count += 1
            self.stdout.write(f'  capture re-run: payment_id={payment.id}')

        self.stdout.write(self.style.SUCCESS(f'{count} capture re-run(s) dispatched.'))
