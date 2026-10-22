"""Card binding without a charge during the free trial (DRF-2989, В-10).

The ONE place that knows HOW a card is saved without moving money.
Variant В3 (main window 21.10): a 1 ₽ two-stage hold with
``save_payment_method`` — YooKassa reports ``waiting_for_capture`` with the
saved method, the method is stored and the hold is released (cancel).
Nothing is captured, so no 54-ФЗ receipt is fiscalised and nothing shows
as a charge.

Switching to В1 (1 ₽ + refund) or В2 (payment method without a payment)
means rewriting this module only: :func:`start_card_binding` (open the
binding at the provider) and :func:`complete_card_binding` /
:func:`release_hold` (the provider's answer). The callers —
``start_card_setup`` and ``handle_webhook_event`` — do not change.

**Go-live:** В3 needs the shop's two-stage payments confirmed by the
owner (YooKassa cabinet). Charges are off until then.

What the binding does NOT do (the trial stays the trial): no invoice,
no change of ``status`` / ``current_period_*``, no activated / renewed
event. The first real charge stays where it is —
``charge_subscriptions_monthly`` on ``trial_ends_at``.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.models import BillingPayment, SpecialistSubscription
from billing.receipts import build_platform_receipt

logger = logging.getLogger(__name__)

#: The hold amount — authorised, never captured.
BINDING_HOLD_AMOUNT = Decimal("1.00")
_DESCRIPTION = "Ayla Pro: привязка карты (без списания)"


def binding_applies(subscription: SpecialistSubscription, *, today) -> bool:
    """The account is in its free trial: a card is saved, not charged.

    ``trial_ends_at`` is None for a cabinet that has not published yet —
    its trial has not even started, so it is not charged either. A
    personal account from before DRF-2984 (no tenant, no trial end)
    keeps the old setup charge: it has no trial end to charge on.
    """
    if subscription.status != SpecialistSubscription.Status.TRIAL:
        return False
    if subscription.trial_ends_at is None:
        return subscription.tenant_id is not None
    return subscription.trial_ends_at > today


def pending_binding(subscription: SpecialistSubscription) -> BillingPayment | None:
    return (
        BillingPayment.objects
        .filter(
            subscription=subscription,
            kind=BillingPayment.Kind.CARD_BINDING,
            status=BillingPayment.Status.PENDING,
        )
        .order_by("-created_at")
        .first()
    )


def start_card_binding(subscription: SpecialistSubscription, *, return_url: str, client):
    """Open (or return the open) binding; returns the payment row.

    Idempotent across days: an unfinished binding is returned as it is —
    a second hold is never opened while one may still be confirmed. The
    attempt number in the key is the count of finished bindings, so two
    racing first calls send the SAME idempotence key to YooKassa and get
    the same provider payment; the unique key settles the local row.
    """
    existing = pending_binding(subscription)
    if existing is not None:
        return existing
    attempt = BillingPayment.objects.filter(
        subscription=subscription, kind=BillingPayment.Kind.CARD_BINDING,
    ).count() + 1
    key = f"bind:{subscription.id}:attempt-{attempt}"
    result = client.create_binding_hold(
        amount=BINDING_HOLD_AMOUNT,
        description=_DESCRIPTION,
        return_url=return_url,
        idempotency_key=key,
        receipt=build_platform_receipt(
            subscription, amount=BINDING_HOLD_AMOUNT, description=_DESCRIPTION,
        ),
        metadata={
            "subscription_id": str(subscription.id),
            "kind": BillingPayment.Kind.CARD_BINDING,
        },
    )
    try:
        with transaction.atomic():
            return BillingPayment.objects.create(
                subscription=subscription,
                kind=BillingPayment.Kind.CARD_BINDING,
                amount=BINDING_HOLD_AMOUNT,
                idempotency_key=key,
                provider_payment_id=result["provider_payment_id"],
                confirmation_url=result["confirmation_url"],
            )
    except IntegrityError:
        return BillingPayment.objects.get(idempotency_key=key)


def complete_card_binding(payment: BillingPayment, *, event: str, info: dict) -> tuple[str, bool]:
    """Apply the provider's answer to a LOCKED binding row.

    Returns ``(status, release)``: ``release`` asks the caller to cancel
    the hold after commit. Never touches the period, the status or the
    dunning counters of the subscription.
    """
    subscription = payment.subscription
    if event == "payment.waiting_for_capture" and info["status"] == "waiting_for_capture":
        method_id = info["payment_method_id"] if info["payment_method_saved"] else ""
        if method_id:
            subscription.payment_method_id = method_id
            subscription.payment_method_saved_at = timezone.now()
            subscription.card_last4 = info["card_last4"]
            subscription.card_brand = info["card_brand"]
            subscription.save(update_fields=[
                "payment_method_id", "payment_method_saved_at",
                "card_last4", "card_brand", "updated_at",
            ])
        else:
            logger.warning(
                "billing.card_binding.authorised_without_saved_method payment=%s",
                payment.pk,
            )
        payment.status = (
            BillingPayment.Status.SUCCEEDED if method_id else BillingPayment.Status.FAILED
        )
        payment.failure_reason = "" if method_id else "method_not_saved"
        payment.save(update_fields=["status", "failure_reason", "updated_at"])
        return "ok", True
    if event == "payment.canceled" and info["status"] == "canceled":
        payment.status = BillingPayment.Status.CANCELED
        payment.failure_reason = "provider_canceled"
        payment.save(update_fields=["status", "failure_reason", "updated_at"])
        return "ok", False
    if event == "payment.succeeded":
        # A hold must never be captured. If it was, a 1 ₽ charge happened
        # outside the design — loud, and left for a human (refund).
        logger.critical(
            "billing.card_binding.captured payment=%s provider=%s",
            payment.pk, payment.provider_payment_id,
        )
    return "ignored", False


def release_hold(payment: BillingPayment, *, client) -> None:
    """Cancel the authorised hold. A failure is logged, not raised: the
    card is already saved, and an uncaptured card hold expires on its
    own at the provider."""
    try:
        client.cancel_payment(
            payment.provider_payment_id,
            idempotency_key=f"cancel:{payment.idempotency_key}",
        )
    except Exception:  # noqa: BLE001 — the binding is done; the hold expires
        logger.exception(
            "billing.card_binding.release_failed payment=%s provider=%s",
            payment.pk, payment.provider_payment_id,
        )
