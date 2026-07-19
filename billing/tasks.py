"""Celery beat tasks for billing (W2). Beat schedule entries are added
by W1 in djangoProject/settings (B-handoff) — proposed:

    "billing-monthly-charge": {
        "task": "billing.tasks.charge_subscriptions_monthly",
        "schedule": crontab(hour=4, minute=30),
    },
    "billing-dunning-retry": {
        "task": "billing.tasks.retry_failed_subscription_charges",
        "schedule": crontab(hour=5, minute=15),
    },
"""
from __future__ import annotations

import logging

import sentry_sdk
from celery import shared_task
from django.utils import timezone

from billing.charges import charge_subscription, retry_open_invoice
from billing.models import SpecialistSubscription

logger = logging.getLogger(__name__)


@shared_task
def charge_subscriptions_monthly() -> int:
    """Monthly recurrent charge for every due subscription (D7).

    Due = current paid period ended + a saved card exists. One broken
    subscription must not block the rest — per-item try/except + Sentry.
    """
    today = timezone.localdate()
    due = (
        SpecialistSubscription.objects
        .filter(
            status__in=(
                SpecialistSubscription.Status.TRIAL,
                SpecialistSubscription.Status.ACTIVE,
            ),
            current_period_end__lt=today,
        )
        .exclude(payment_method_id="")
        .select_related("tariff", "user")
    )
    charged = 0
    for subscription in due.iterator():
        try:
            if charge_subscription(subscription=subscription, today=today) is not None:
                charged += 1
        except Exception as exc:  # noqa: BLE001 — keep the batch alive
            logger.exception("billing.monthly_charge.failed sub=%s", subscription.id)
            sentry_sdk.capture_exception(exc)
    logger.info("billing.monthly_charge.done due=%s charged=%s", due.count(), charged)
    return charged


@shared_task
def retry_failed_subscription_charges() -> int:
    """Dunning retries: subscriptions whose next_retry_at has arrived."""
    now = timezone.now()
    due = (
        SpecialistSubscription.objects
        .filter(
            status__in=(
                SpecialistSubscription.Status.TRIAL,
                SpecialistSubscription.Status.ACTIVE,
            ),
            next_retry_at__isnull=False,
            next_retry_at__lte=now,
        )
        .exclude(payment_method_id="")
    )
    retried = 0
    for subscription in due.iterator():
        try:
            if retry_open_invoice(subscription=subscription):
                retried += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("billing.dunning_retry.failed sub=%s", subscription.id)
            sentry_sdk.capture_exception(exc)
    logger.info("billing.dunning_retry.done due=%s ok=%s", due.count(), retried)
    return retried
