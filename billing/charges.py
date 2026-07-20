"""Money orchestration — card setup, monthly charge, dunning (D7).

Flow (AYLA-DEC-0007):
1. ``start_card_setup`` — first payment with save_payment_method:true;
   the master confirms in the browser (confirmation_url).
2. YooKassa webhook ``payment.succeeded`` → ``handle_webhook_event`` →
   the payment_method_id lands on the subscription (trial → active,
   ``subscription.activated`` emitted).
3. Beat task → ``charge_subscription`` — monthly recurrent charge =
   tariff + pending BookingFees; success advances the period.
4. Dunning: failure → retry T+1d, T+3d → ``past_due`` +
   ``subscription.past_due`` (which C1 then enforces on new bookings).

Idempotency: invoice key ``charge:{subscription}:{period-start}`` (one
invoice per period), payment key ``pay:{invoice-key}:attempt-N`` (one
provider call per attempt row).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import sentry_sdk
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from billing import events
from billing.models import (
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    BookingFee,
    SpecialistSubscription,
    TariffPlan,
    quantize_money,
)
from billing.receipts import build_platform_receipt
from billing.yookassa import (
    BillingPaymentClientError,
    BillingPaymentConfigError,
    BillingYooKassaClient,
)

logger = logging.getLogger(__name__)

# Dunning schedule (W2 brief): fail → retry T+1d, T+3d → past_due.
RETRY_DELAYS = (timedelta(days=1), timedelta(days=3))


def _offer_version() -> str:
    # TODO(legal): B-6 — final offer text + version due from legal week 2.
    return getattr(settings, "BILLING_OFFER_VERSION", "offer-0.0-todo-legal")


def _period_end(start) -> object:
    return start + relativedelta(months=1) - timedelta(days=1)


def _subscription_description(subscription: SpecialistSubscription, period_start) -> str:
    return (
        f"Подписка Ayla Pro ({subscription.tariff.code}), "
        f"{period_start:%d.%m.%Y}"
    )


@dataclass(frozen=True, slots=True)
class CardSetupResult:
    subscription_id: UUID
    invoice_id: UUID
    confirmation_url: str


def get_or_create_subscription(
    *, user, tariff_code: str, tenant=None,
) -> SpecialistSubscription:
    """Fetch the billing account or start a trial one.

    Trial ends on the first successful charge (webhook flips status to
    active). Personal account: tenant=None; salon account: tenant set.
    """
    tariff = TariffPlan.objects.get(code=tariff_code, is_active=True)
    lookup = {"tenant": tenant} if tenant is not None else {
        "user": user, "tenant__isnull": True,
    }
    subscription = SpecialistSubscription.objects.filter(**lookup).first()
    if subscription is not None:
        return subscription
    return SpecialistSubscription.objects.create(
        user=user, tenant=tenant, tariff=tariff,
        status=SpecialistSubscription.Status.TRIAL,
    )


def start_card_setup(
    *, user, tariff_code: str, return_url: str, tenant=None, client=None,
) -> CardSetupResult:
    """Create the first invoice + YooKassa setup payment (card binding).

    Idempotent: a repeat call on the same day returns the existing
    invoice's confirmation_url (unique keys on invoice + payment).
    """
    with transaction.atomic():
        subscription = get_or_create_subscription(
            user=user, tariff_code=tariff_code, tenant=tenant,
        )
        # D7 legal: record the auto-charge consent (one active per user).
        if not BillingConsent.objects.filter(
            user=user, revoked_at__isnull=True,
        ).exists():
            BillingConsent.objects.create(
                user=user, document_version=_offer_version(),
            )
        period_start = timezone.localdate()
        invoice, _ = BillingInvoice.objects.get_or_create(
            idempotency_key=f"setup:{subscription.id}:{period_start.isoformat()}",
            defaults={
                "subscription": subscription,
                "period_start": period_start,
                "period_end": _period_end(period_start),
                "subscription_amount": subscription.tariff.price,
                "fees_amount": 0,
                "total_amount": subscription.tariff.price,
            },
        )
        existing = BillingPayment.objects.filter(
            idempotency_key=f"pay:{invoice.idempotency_key}",
        ).first()
        if existing is not None:
            return CardSetupResult(subscription.id, invoice.id, existing.confirmation_url)

    # Provider call outside the transaction (network I/O in a lock is an
    # anti-pattern; the unique payment key is the race backstop).
    client = client or BillingYooKassaClient()
    result = client.create_setup_payment(
        amount=invoice.total_amount,
        description=_subscription_description(subscription, invoice.period_start),
        return_url=return_url,
        idempotency_key=f"pay:{invoice.idempotency_key}",
        receipt=build_platform_receipt(
            subscription, amount=invoice.total_amount,
            description=_subscription_description(subscription, invoice.period_start),
        ),
        metadata={
            "subscription_id": str(subscription.id),
            "invoice_id": str(invoice.id),
            "kind": "setup",
        },
    )
    payment = BillingPayment.objects.create(
        invoice=invoice,
        kind=BillingPayment.Kind.SETUP,
        amount=invoice.total_amount,
        idempotency_key=f"pay:{invoice.idempotency_key}",
        provider_payment_id=result["provider_payment_id"],
        confirmation_url=result["confirmation_url"],
    )
    return CardSetupResult(subscription.id, invoice.id, payment.confirmation_url)


def charge_subscription(
    *, subscription: SpecialistSubscription, today=None, client=None,
) -> BillingInvoice | None:
    """Monthly recurrent charge: tariff + all pending BookingFees.

    Due when the current paid period has ended. Returns the invoice, or
    None when the subscription is not chargeable / not due. Failures go
    through the dunning path (register_charge_failure).
    """
    today = today or timezone.localdate()
    if subscription.status not in (
        SpecialistSubscription.Status.TRIAL,
        SpecialistSubscription.Status.ACTIVE,
    ):
        return None
    if not subscription.payment_method_id:
        return None
    if (
        not subscription.current_period_end
        or subscription.current_period_end >= today
    ):
        return None

    period_start = subscription.current_period_end + timedelta(days=1)
    with transaction.atomic():
        invoice, created = BillingInvoice.objects.get_or_create(
            idempotency_key=f"charge:{subscription.id}:{period_start.isoformat()}",
            defaults={
                "subscription": subscription,
                "period_start": period_start,
                "period_end": _period_end(period_start),
                "subscription_amount": subscription.tariff.price,
                "fees_amount": 0,
                "total_amount": subscription.tariff.price,
            },
        )
        if created:
            fees = BookingFee.objects.filter(
                subscription=subscription, status=BookingFee.Status.PENDING,
            )
            fees_amount = quantize_money(
                fees.aggregate(total=Sum("amount"))["total"] or 0,
            )
            invoice.fees_amount = fees_amount
            invoice.total_amount = quantize_money(
                invoice.subscription_amount + fees_amount,
            )
            invoice.save(update_fields=["fees_amount", "total_amount", "updated_at"])
            fees.update(status=BookingFee.Status.INVOICED, invoice=invoice)
        else:
            # The invoice for this period already exists — do NOT
            # re-charge it from the monthly sweep (a pending provider
            # payment would double-charge). Retries belong to the
            # dunning path (retry_open_invoice).
            return None

    attempt = invoice.payments.count() + 1
    client = client or BillingYooKassaClient()
    description = _subscription_description(subscription, invoice.period_start)
    try:
        result = client.create_recurrent_payment(
            amount=invoice.total_amount,
            payment_method_id=subscription.payment_method_id,
            description=description,
            idempotency_key=f"pay:{invoice.idempotency_key}:attempt-{attempt}",
            receipt=build_platform_receipt(
                subscription, amount=invoice.total_amount, description=description,
            ),
            metadata={
                "subscription_id": str(subscription.id),
                "invoice_id": str(invoice.id),
                "kind": "recurrent",
            },
        )
    except (BillingPaymentClientError, BillingPaymentConfigError) as exc:
        logger.warning(
            "billing.charge.provider_error subscription=%s err=%s",
            subscription.id, exc,
        )
        register_charge_failure(subscription=subscription, invoice=invoice, reason=str(exc))
        return None

    payment_row = BillingPayment.objects.create(
        invoice=invoice,
        kind=BillingPayment.Kind.RECURRENT,
        amount=invoice.total_amount,
        idempotency_key=f"pay:{invoice.idempotency_key}:attempt-{attempt}",
        provider_payment_id=result["provider_payment_id"],
    )
    if result["status"] == "succeeded":
        settle_charge_success(
            subscription=subscription, invoice=invoice, payment_row=payment_row,
        )
    elif result["status"] == "canceled":
        register_charge_failure(
            subscription=subscription, invoice=invoice,
            payment_row=payment_row, reason="provider_canceled",
        )
    # else "pending" — the webhook settles it (async confirmation).
    return invoice


def settle_charge_success(
    *, subscription: SpecialistSubscription, invoice: BillingInvoice,
    payment_row: BillingPayment, payment_method_id: str = "",
) -> None:
    """Mark everything paid and advance the billing period."""
    with transaction.atomic():
        payment_row.status = BillingPayment.Status.SUCCEEDED
        payment_row.save(update_fields=["status", "updated_at"])
        invoice.status = BillingInvoice.Status.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])
        invoice.booking_fees.filter(
            status=BookingFee.Status.INVOICED,
        ).update(status=BookingFee.Status.CHARGED)

        subscription.current_period_start = invoice.period_start
        subscription.current_period_end = invoice.period_end
        subscription.failed_attempts = 0
        subscription.next_retry_at = None
        if payment_method_id:
            subscription.payment_method_id = payment_method_id
            subscription.payment_method_saved_at = timezone.now()
        became_active = subscription.status == SpecialistSubscription.Status.TRIAL
        subscription.status = SpecialistSubscription.Status.ACTIVE
        subscription.save()

    if became_active:
        try:
            events.emit_subscription_activated(subscription)
        except Exception as exc:  # topics may be unregistered pre-W1-patch
            logger.exception("billing.activated.emit_failed sub=%s", subscription.id)
            sentry_sdk.capture_exception(exc)


def register_charge_failure(
    *, subscription: SpecialistSubscription, invoice: BillingInvoice,
    payment_row: BillingPayment | None = None, reason: str = "",
) -> None:
    """Dunning: retry T+1d, T+3d, then past_due (+ C4 event)."""
    became_past_due = False
    with transaction.atomic():
        if payment_row is not None:
            payment_row.status = BillingPayment.Status.FAILED
            payment_row.failure_reason = reason[:200]
            payment_row.save(
                update_fields=["status", "failure_reason", "updated_at"],
            )
        subscription.failed_attempts += 1
        attempts = subscription.failed_attempts
        if attempts <= len(RETRY_DELAYS):
            subscription.next_retry_at = timezone.now() + RETRY_DELAYS[attempts - 1]
            subscription.save(
                update_fields=["failed_attempts", "next_retry_at", "updated_at"],
            )
        else:
            subscription.next_retry_at = None
            subscription.status = SpecialistSubscription.Status.PAST_DUE
            subscription.save(
                update_fields=["failed_attempts", "next_retry_at", "status", "updated_at"],
            )
            invoice.status = BillingInvoice.Status.FAILED
            invoice.save(update_fields=["status", "updated_at"])
            became_past_due = True

    if became_past_due:
        debt = quantize_money(
            subscription.invoices.filter(
                status=BillingInvoice.Status.FAILED,
            ).aggregate(total=Sum("total_amount"))["total"] or 0,
        )
        try:
            events.emit_subscription_past_due(
                subscription, debt_amount=debt, failed_attempts=attempts,
            )
        except Exception as exc:
            logger.exception("billing.past_due.emit_failed sub=%s", subscription.id)
            sentry_sdk.capture_exception(exc)


def retry_open_invoice(*, subscription: SpecialistSubscription, client=None) -> bool:
    """Re-attempt the latest open invoice (dunning retry task)."""
    invoice = (
        subscription.invoices
        .filter(status=BillingInvoice.Status.OPEN)
        .order_by("-created_at")
        .first()
    )
    if invoice is None:
        subscription.next_retry_at = None
        subscription.save(update_fields=["next_retry_at", "updated_at"])
        return False
    attempt = invoice.payments.count() + 1
    client = client or BillingYooKassaClient()
    description = _subscription_description(subscription, invoice.period_start)
    try:
        result = client.create_recurrent_payment(
            amount=invoice.total_amount,
            payment_method_id=subscription.payment_method_id,
            description=description,
            idempotency_key=f"pay:{invoice.idempotency_key}:attempt-{attempt}",
            receipt=build_platform_receipt(
                subscription, amount=invoice.total_amount, description=description,
            ),
            metadata={
                "subscription_id": str(subscription.id),
                "invoice_id": str(invoice.id),
                "kind": "recurrent",
            },
        )
    except (BillingPaymentClientError, BillingPaymentConfigError) as exc:
        register_charge_failure(subscription=subscription, invoice=invoice, reason=str(exc))
        return False

    payment_row = BillingPayment.objects.create(
        invoice=invoice,
        kind=BillingPayment.Kind.RECURRENT,
        amount=invoice.total_amount,
        idempotency_key=f"pay:{invoice.idempotency_key}:attempt-{attempt}",
        provider_payment_id=result["provider_payment_id"],
    )
    if result["status"] == "succeeded":
        settle_charge_success(
            subscription=subscription, invoice=invoice, payment_row=payment_row,
        )
        return True
    register_charge_failure(
        subscription=subscription, invoice=invoice,
        payment_row=payment_row, reason=f"provider_{result['status']}",
    )
    return False


class NoDebtError(Exception):
    """No outstanding debt — the pay-debt endpoint maps this to 409."""


def find_outstanding_invoice(
    subscription: SpecialistSubscription,
) -> BillingInvoice | None:
    """The invoice constituting the current debt (FAILED or still OPEN),
    or None when the subscription owes nothing."""
    return (
        subscription.invoices
        .filter(
            status__in=(
                BillingInvoice.Status.FAILED,
                BillingInvoice.Status.OPEN,
            ),
        )
        .order_by("-created_at")
        .first()
    )


@dataclass(frozen=True, slots=True)
class DebtPaymentResult:
    payment_id: UUID
    invoice_id: UUID
    provider_payment_id: str
    confirmation_url: str  # "" when charged instantly via the saved method
    amount: Decimal
    status: str


def pay_debt(
    *, subscription: SpecialistSubscription, return_url: str = "", client=None,
) -> DebtPaymentResult:
    """One-shot collection of the outstanding debt (the past_due path).

    Debt = the latest unpaid invoice (FAILED or still OPEN) + any fees
    accrued after it was issued (PENDING). Charged instantly via the
    saved payment method when one exists; otherwise a redirect payment
    with save_payment_method (re-binds the card, D7).

    Idempotent: an in-flight (PENDING) debt payment is returned
    unchanged; once the invoice is settled a replay raises NoDebtError.
    On instant success the subscription returns to active (C1 unblocks)
    via settle_charge_success.
    """
    invoice = find_outstanding_invoice(subscription)
    if invoice is None:
        raise NoDebtError("Subscription has no outstanding debt")

    with transaction.atomic():
        # Fees accrued after the invoice was issued join the debt charge.
        pending = BookingFee.objects.filter(
            subscription=subscription, status=BookingFee.Status.PENDING,
        )
        extra = quantize_money(
            pending.aggregate(total=Sum("amount"))["total"] or 0,
        )
        if extra:
            invoice.fees_amount = quantize_money(invoice.fees_amount + extra)
            invoice.total_amount = quantize_money(
                invoice.subscription_amount + invoice.fees_amount,
            )
            invoice.save(
                update_fields=["fees_amount", "total_amount", "updated_at"],
            )
            pending.update(status=BookingFee.Status.INVOICED, invoice=invoice)
        if invoice.status == BillingInvoice.Status.FAILED:
            invoice.status = BillingInvoice.Status.OPEN
            invoice.save(update_fields=["status", "updated_at"])

        # Idempotent replay: return the in-flight provider payment.
        inflight = (
            invoice.payments
            .filter(status=BillingPayment.Status.PENDING)
            .order_by("-created_at")
            .first()
        )
        if inflight is not None:
            return DebtPaymentResult(
                payment_id=inflight.id,
                invoice_id=invoice.id,
                provider_payment_id=inflight.provider_payment_id,
                confirmation_url=inflight.confirmation_url,
                amount=inflight.amount,
                status=inflight.status,
            )

    attempt = invoice.payments.count() + 1
    client = client or BillingYooKassaClient()
    description = f"Оплата долга Ayla Pro ({subscription.tariff.code})"
    metadata = {
        "subscription_id": str(subscription.id),
        "invoice_id": str(invoice.id),
        "kind": "debt",
    }
    idempotency_key = f"pay:{invoice.idempotency_key}:attempt-{attempt}"
    receipt = build_platform_receipt(
        subscription, amount=invoice.total_amount, description=description,
    )
    # Provider errors (config/client) propagate — the view maps them.
    if subscription.payment_method_id:
        result = client.create_recurrent_payment(
            amount=invoice.total_amount,
            payment_method_id=subscription.payment_method_id,
            description=description,
            idempotency_key=idempotency_key,
            receipt=receipt,
            metadata=metadata,
        )
        confirmation_url = ""
    else:
        result = client.create_setup_payment(
            amount=invoice.total_amount,
            description=description,
            return_url=return_url,
            idempotency_key=idempotency_key,
            receipt=receipt,
            metadata=metadata,
        )
        confirmation_url = result["confirmation_url"]

    payment_row = BillingPayment.objects.create(
        invoice=invoice,
        kind=BillingPayment.Kind.RECURRENT,
        amount=invoice.total_amount,
        idempotency_key=idempotency_key,
        provider_payment_id=result["provider_payment_id"],
        confirmation_url=confirmation_url,
    )
    status = result["status"]
    if status == "succeeded":
        settle_charge_success(
            subscription=subscription, invoice=invoice, payment_row=payment_row,
        )
        status = BillingPayment.Status.SUCCEEDED
    elif status == "canceled":
        register_charge_failure(
            subscription=subscription, invoice=invoice,
            payment_row=payment_row, reason="provider_canceled",
        )
        status = BillingPayment.Status.FAILED
    # else "pending" — the webhook settles it.
    return DebtPaymentResult(
        payment_id=payment_row.id,
        invoice_id=invoice.id,
        provider_payment_id=result["provider_payment_id"],
        confirmation_url=confirmation_url,
        amount=invoice.total_amount,
        status=status,
    )


def handle_webhook_event(
    *, event: str, provider_payment_id: str, client=None,
) -> str:
    """Apply a verified YooKassa webhook to the billing payment.

    Returns "ok" / "duplicate" / "ignored". Idempotent: an already-
    succeeded payment acks as duplicate; unknown payments ack silently
    (could be a test notification from the YooKassa console).
    """
    payment_row = (
        BillingPayment.objects
        .filter(provider_payment_id=provider_payment_id)
        .select_related("invoice", "invoice__subscription")
        .first()
    )
    if payment_row is None:
        return "ok"

    client = client or BillingYooKassaClient()
    info = client.get_payment_info(provider_payment_id)

    with transaction.atomic():
        # NOTE: no select_related here — Postgres rejects FOR UPDATE on
        # the nullable side of an outer join (invoice FK is nullable).
        locked = (
            BillingPayment.objects
            .select_for_update()
            .get(pk=payment_row.pk)
        )
        if locked.status == BillingPayment.Status.SUCCEEDED:
            return "duplicate"
        invoice = locked.invoice
        if invoice is None:
            # BillingPayments always carry an invoice in our flows; a
            # NULL here is data drift — ack, don't 500 into retries.
            logger.error(
                "billing.webhook.payment_without_invoice id=%s", locked.pk,
            )
            return "ignored"
        subscription = invoice.subscription

        if event == "payment.succeeded" and info["status"] == "succeeded":
            method_id = info["payment_method_id"] if info["payment_method_saved"] else ""
            settle_charge_success(
                subscription=subscription, invoice=invoice,
                payment_row=locked, payment_method_id=method_id,
            )
        elif event == "payment.canceled" and info["status"] == "canceled":
            register_charge_failure(
                subscription=subscription, invoice=invoice,
                payment_row=locked, reason="provider_canceled",
            )
        else:
            return "ignored"
    return "ok"
