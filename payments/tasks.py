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
from django.db import transaction
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
