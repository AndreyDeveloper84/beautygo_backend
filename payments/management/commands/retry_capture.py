"""Manual capture re-run for stuck two-stage payments (D9, ADR §2).

Usage:
    # dry run — lists what would be captured, charges nothing
    python manage.py retry_capture --operator ops
    python manage.py retry_capture --operator ops --payment-id <uuid>

    # the real run — names the database it is meant for
    python manage.py retry_capture --operator ops --database <name> --apply
    python manage.py retry_capture --operator ops --database <name> --apply --sync

"Stuck" = authorized (held) payments whose capture is scheduled or has
failed and whose planned capture time has arrived (or was never set —
e.g. the schedule hook failed after complete()). The task itself is
idempotent (stable YooKassa key ``capture-{payment.id}`` + local state
guard), so a repeated run does not charge twice.

A capture is a charge to the client and cannot be taken back, so the
command does nothing without ``--apply`` (DRF-2689):

* without ``--apply`` it prints the payments, their count and total, the
  database this process is connected to and the provider mode — and
  dispatches no capture;
* ``--apply`` requires ``--database`` and refuses, before the first
  capture, unless it names the connected database;
* every run, dry or real, leaves one ``retry_capture_run`` receipt. On a
  real run it is written BEFORE the first capture: no receipt, no charge.
  It records intent (which payments, how much); the outcome of each stays
  in ``Payment.capture_state``.

See ``payments/money_commands.py`` for why the guard has this shape.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from analytics import event_catalogue
from payments.models import Payment
from payments.money_commands import (
    ReceiptUnwritten,
    clean_operator,
    provider_mode,
    require_named_database,
    write_receipt,
)
from payments.tasks import capture_payment_task

logger = logging.getLogger(__name__)

COMMAND = "retry_capture"


class Command(BaseCommand):
    help = (
        "Re-run capture for stuck held payments (waiting_for_capture). "
        "Without --apply — a dry run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--payment-id', type=str, default=None,
            help='Retry capture for a single Payment UUID.',
        )
        parser.add_argument(
            '--sync', action='store_true',
            help='Run the capture task inline instead of enqueueing.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Capture. Without it nothing is charged — the run is a dry run.',
        )
        parser.add_argument(
            '--database', type=str, default=None,
            help=(
                'Name of the database you mean to charge in. Required with '
                '--apply; must be the one this process is connected to.'
            ),
        )
        parser.add_argument(
            '--operator', required=True,
            help=(
                'Who runs it — a role or a tag, not a name; goes into the '
                'run receipt.'
            ),
        )

    def handle(self, *args, **options):
        apply = options['apply']
        operator = clean_operator(options['operator'], command=COMMAND, required=True)
        database = require_named_database(
            options['database'], command=COMMAND, required=apply,
        )

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

        # One read feeds the report, the receipt and the dispatch, so the
        # three cannot name different payments.
        stuck = list(qs.order_by('pk').values_list('id', 'amount', 'capture_state'))
        total = sum((amount for _id, amount, _state in stuck), Decimal('0'))
        mode = provider_mode()

        self.stdout.write(
            f'=== {"CAPTURE" if apply else "DRY RUN"} · database={database} · '
            f'provider={mode} ==='
        )
        for payment_id, amount, state in stuck:
            self.stdout.write(
                f'  {"capture re-run" if apply else "would capture"}: '
                f'payment_id={payment_id} amount={amount} capture_state={state}'
            )
        self.stdout.write(f'{len(stuck)} stuck payment(s), total {total}.')

        receipt = {
            'operator': operator,
            'mode': 'apply' if apply else 'dry_run',
            'database': database,
            'provider_mode': mode,
            'dispatch': 'sync' if options['sync'] else 'async',
            'scope': 'single' if options['payment_id'] else 'all',
            'matched': len(stuck),
            'amount_total': str(total),
            'payment_ids': [str(payment_id) for payment_id, _amount, _state in stuck],
        }

        if not apply:
            # Nothing to lose on a dry run — its receipt is best-effort.
            try:
                write_receipt(event_catalogue.RETRY_CAPTURE_RUN, receipt)
            except Exception:  # noqa: BLE001 — a dry run is not failed by its receipt
                logger.exception('payments.retry_capture.receipt_write_failed')
                self.stderr.write('dry-run receipt not written (see the log)')
            self.stdout.write(self.style.WARNING(
                'dry run: nothing captured (--database <name> --apply captures)'
            ))
            return

        # Fail-closed, and BEFORE the first capture: the charge is external
        # and cannot be rolled back together with a receipt that failed.
        try:
            write_receipt(event_catalogue.RETRY_CAPTURE_RUN, receipt)
        except Exception as exc:  # noqa: BLE001 — any receipt failure is a refusal
            raise ReceiptUnwritten(
                'run receipt not written — NOTHING was captured '
                f'({type(exc).__name__})'
            ) from exc

        for payment_id, _amount, _state in stuck:
            if options['sync']:
                capture_payment_task(str(payment_id))
            else:
                capture_payment_task.apply_async(args=[str(payment_id)])

        self.stdout.write(self.style.SUCCESS(
            f'{len(stuck)} capture re-run(s) dispatched.'
        ))
