"""Celery tasks for payments — deferred two-stage capture (D9).

The capture task is the mechanism behind the capture-strategy ADR:
``complete()`` enqueues it with a countdown derived from
``compute_capture_at`` (pilot default: immediately). It is deliberately
NOT the only safety net — the reconciliation job plus the
``retry_capture`` management command cover worker outages.

Idempotency contract: the YooKassa idempotency key is STABLE per
payment (``capture-{payment.id}``), so any number of task deliveries /
manual retries issue at most one provider-side capture. Locally the
task is a no-op unless the payment is still AUTHORIZED + scheduled, so
a late duplicate delivery after a successful capture does nothing.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .exceptions import PaymentClientError, PaymentConfigError

logger = logging.getLogger(__name__)

# Retry budget: 5 retries with exponential backoff (1m, 2m, 4m, 8m, 16m
# + jitter) — comfortably inside the shortest YooKassa hold (2h) even
# with the safety buffer. After the budget is exhausted the payment
# lands in capture_state=capture_failed and the reconciliation job /
# alerts take over (ADR §2).
CAPTURE_MAX_RETRIES = 5


@shared_task(
    bind=True,
    autoretry_for=(PaymentClientError,),
    retry_backoff=60,
    retry_backoff_max=16 * 60,
    retry_jitter=True,
    retry_kwargs={'max_retries': CAPTURE_MAX_RETRIES},
    # A provider misconfiguration (empty credentials) is not transient —
    # retrying cannot fix it; fail straight to capture_failed.
    dont_autoretry_for=(PaymentConfigError,),
)
def capture_payment_task(self, payment_id: str) -> None:
    from payments.models import Payment
    from payments.services import YooKassaService

    try:
        with transaction.atomic():
            payment = (
                Payment.objects
                .select_for_update()
                .select_related('appointment')
                .get(pk=payment_id)
            )
            # Idempotent no-op: only a held payment whose capture is
            # still due qualifies. SCHEDULED is the normal path;
            # CAPTURE_FAILED is the retry-command / reconciliation
            # re-entry. Anything else (already captured, canceled by a
            # booking cancel, retried after success) exits silently.
            if not (
                payment.status == Payment.Status.AUTHORIZED
                and payment.capture_state in (
                    Payment.CaptureState.SCHEDULED,
                    Payment.CaptureState.CAPTURE_FAILED,
                )
            ):
                logger.info(
                    'capture.skip payment_id=%s status=%s capture_state=%s',
                    payment.id, payment.status, payment.capture_state,
                )
                return

            svc = YooKassaService()
            result = svc.capture_payment(
                provider_payment_id=payment.provider_payment_id,
                amount=payment.amount,
                idempotency_key=f'capture-{payment.id}',
            )

            if result.get('status') == 'succeeded':
                payment.status = Payment.Status.PAID
                payment.capture_state = (
                    Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
                )
                payment.captured_at = timezone.now()
                payment.save(update_fields=[
                    'status', 'capture_state', 'captured_at', 'updated_at',
                ])
                # NOTE: the payment.captured outbox event is emitted by
                # the webhook handler (single emit site), NOT here — a
                # double emit with distinct event_ids would defeat
                # consumer-side dedupe. If webhooks are disabled the
                # reconciliation job converges state instead.
                logger.info(
                    'capture.succeeded payment_id=%s appointment_id=%s',
                    payment.id, payment.appointment_id,
                )
            else:
                # Unexpected provider state (e.g. canceled externally).
                # Do not retry blindly — surface via capture_failed for
                # reconciliation to re-read provider state.
                payment.capture_state = Payment.CaptureState.CAPTURE_FAILED
                payment.save(update_fields=['capture_state', 'updated_at'])
                logger.warning(
                    'capture.unexpected_state payment_id=%s provider_status=%s',
                    payment.id, result.get('status'),
                )
    except PaymentConfigError:
        # Missing credentials — not transient. Mark for ops and return
        # WITHOUT re-raising: in eager mode (tests) a raise would
        # propagate into the complete() request that enqueued us, and in
        # production retrying cannot fix env wiring.
        with transaction.atomic():
            Payment.objects.filter(pk=payment_id).update(
                capture_state=Payment.CaptureState.CAPTURE_FAILED,
                updated_at=timezone.now(),
            )
        logger.error(
            'capture.config_error payment_id=%s — YooKassa not configured',
            payment_id,
        )
        return
    except PaymentClientError:
        if self.request.retries >= CAPTURE_MAX_RETRIES:
            # Retry budget exhausted — pin the incident state for
            # reconciliation and return without re-raising (same eager-
            # mode propagation rationale as above; the state + logs carry
            # the incident, not the caller's stack trace).
            with transaction.atomic():
                Payment.objects.filter(pk=payment_id).update(
                    capture_state=Payment.CaptureState.CAPTURE_FAILED,
                    updated_at=timezone.now(),
                )
            logger.error(
                'capture.failed_permanently payment_id=%s after %d retries',
                payment_id, self.request.retries,
            )
            return
        raise


# ---------------------------------------------------------------------------
# Reconciliation (D9, ADR §2) — periodic safety net for the capture loop
# ---------------------------------------------------------------------------

def _ops_alert(message: str, **ctx) -> None:
    """Ops-facing alert: structured log + Sentry (no-op when the DSN is
    not configured — dev/test). NEVER carries PII or payment details
    beyond ids and state enums."""
    logger.error('%s %s', message, ctx)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f'{message} {ctx}', level='error',
        )
    except Exception:  # noqa: BLE001 — sentry uninstalled/uninitialized
        pass


@shared_task
def reconcile_captures() -> dict:
    """Find and repair stuck two-stage payments. Runs from Celery Beat.

    Three incident classes (D9 ADR §2):

    1. ``completed_stuck`` — the visit is completed but the payment is
       still held past its planned capture time (worker outage, failed
       schedule hook). Action: re-enqueue the capture task + alert.
    2. ``expiry_approaching`` — a hold is within 2× the safety buffer of
       its YooKassa ``expires_at``. After that moment the hold
       auto-cancels (``expired_on_capture``) and the money unfreezes
       without a capture — the worst silent loss. Action: alert; if the
       appointment is already completed, ALSO capture proactively.
    3. ``capture_failed`` — the retry budget was exhausted. Action:
       alert (ops replays via the retry_capture command after fixing
       the cause).
    """
    from appointments.models import Appointment
    from payments.models import Payment

    now = timezone.now()
    buffer_min = int(getattr(settings, 'CAPTURE_SAFETY_BUFFER_MINUTES', 60))
    stats = {'completed_stuck': 0, 'expiry_approaching': 0, 'capture_failed': 0}

    # 1. completed visit, capture overdue (or never scheduled) → heal.
    stuck = (
        Payment.objects
        .select_related('appointment')
        .filter(
            status=Payment.Status.AUTHORIZED,
            capture_state=Payment.CaptureState.SCHEDULED,
            appointment__status=Appointment.Status.COMPLETED,
        )
        .filter(
            Q(capture_scheduled_for__lt=now)
            | Q(capture_scheduled_for__isnull=True)
        )
    )
    for payment in stuck.iterator():
        capture_payment_task.apply_async(args=[str(payment.id)])
        stats['completed_stuck'] += 1
        _ops_alert(
            'reconcile.completed_stuck_in_waiting_for_capture',
            payment_id=str(payment.id),
            appointment_id=str(payment.appointment_id),
        )

    # 2. Hold nearing its provider deadline.
    from datetime import timedelta as _td
    expiry_threshold = now + _td(minutes=2 * buffer_min)
    expiring = (
        Payment.objects
        .select_related('appointment')
        .filter(
            status=Payment.Status.AUTHORIZED,
            capture_state=Payment.CaptureState.SCHEDULED,
            yookassa_expires_at__isnull=False,
            yookassa_expires_at__lte=expiry_threshold,
        )
    )
    for payment in expiring.iterator():
        stats['expiry_approaching'] += 1
        _ops_alert(
            'reconcile.hold_expires_at_approaching',
            payment_id=str(payment.id),
            appointment_id=str(payment.appointment_id),
            expires_at=payment.yookassa_expires_at.isoformat(),
        )
        if payment.appointment.status == Appointment.Status.COMPLETED:
            # The visit is done — capture NOW rather than losing the
            # hold to expired_on_capture. Idempotent with bucket 1.
            capture_payment_task.apply_async(args=[str(payment.id)])

    # 3. Retry budget exhausted — needs a human (retry_capture command).
    failed = Payment.objects.filter(
        status=Payment.Status.AUTHORIZED,
        capture_state=Payment.CaptureState.CAPTURE_FAILED,
    )
    for payment in failed.iterator():
        stats['capture_failed'] += 1
        _ops_alert(
            'reconcile.capture_failed',
            payment_id=str(payment.id),
            appointment_id=str(payment.appointment_id),
        )

    if any(stats.values()):
        logger.warning('reconcile.captures stats=%s', stats)
    return stats
