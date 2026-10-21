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

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from billing import events
from billing.flags import require_charges_enabled
from billing.pricing import period_price
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

FROZEN_PAST_DUE_TIMEOUT = "past_due_timeout"
FROZEN_TRIAL_ENDED_NO_CARD = "trial_ended_no_card"

# Dunning schedule (W2 brief): fail → retry T+1d, T+3d → frozen (DRF-2984).
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


def start_trial(
    subscription: SpecialistSubscription, *, started_on,
) -> SpecialistSubscription:
    """Open the free trial month (DRF-2984, owner В-5/В-10).

    Date-to-date: started on the 20th → the first charge is due on the
    20th ``tariff.trial_months`` later. The trial is the first period, so
    the monthly sweep charges a saved card on ``trial_ends_at`` and
    :func:`freeze_ended_trials` freezes the account without one.
    Idempotent: a subscription whose trial already has an end keeps it.
    """
    if subscription.trial_ends_at is not None:
        return subscription
    trial_ends_at = started_on + relativedelta(
        months=subscription.tariff.trial_months,
    )
    with transaction.atomic():
        subscription.status = SpecialistSubscription.Status.TRIAL
        subscription.trial_ends_at = trial_ends_at
        subscription.current_period_start = started_on
        subscription.current_period_end = trial_ends_at - timedelta(days=1)
        subscription.save(update_fields=[
            "status", "trial_ends_at", "current_period_start",
            "current_period_end", "updated_at",
        ])
        events.emit_subscription_trial_started(subscription)
    return subscription


def freeze_ended_trials(*, today=None) -> int:
    """Trial over and no card saved → soft freeze at once (owner В-6).

    Only trials with a known end: rows from before DRF-2984 have no
    ``trial_ends_at`` and stay as they were.
    """
    today = today or timezone.localdate()
    ended = (
        SpecialistSubscription.objects
        .filter(
            status=SpecialistSubscription.Status.TRIAL,
            trial_ends_at__isnull=False,
            trial_ends_at__lte=today,
            payment_method_id="",
        )
        .values_list("pk", flat=True)
    )
    frozen = 0
    for pk in list(ended):
        with transaction.atomic():
            # Re-check under the lock: a card may have landed meanwhile.
            subscription = (
                SpecialistSubscription.objects.select_for_update().get(pk=pk)
            )
            if (
                subscription.status != SpecialistSubscription.Status.TRIAL
                or subscription.payment_method_id
            ):
                continue
            subscription.status = SpecialistSubscription.Status.FROZEN
            subscription.frozen_at = timezone.now()
            subscription.frozen_reason = FROZEN_TRIAL_ENDED_NO_CARD
            subscription.save(update_fields=[
                "status", "frozen_at", "frozen_reason", "updated_at",
            ])
            events.emit_subscription_frozen(subscription)
            frozen += 1
    return frozen


def start_card_setup(
    *, user, tariff_code: str, return_url: str, tenant=None, client=None,
) -> CardSetupResult:
    """Create the first invoice + YooKassa setup payment (card binding).

    Idempotent: a repeat call on the same day returns the existing
    invoice's confirmation_url (unique keys on invoice + payment).
    """
    require_charges_enabled()  # DRF-2984: before any row or provider call
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
        price = period_price(subscription)
        invoice, _ = BillingInvoice.objects.get_or_create(
            idempotency_key=f"setup:{subscription.id}:{period_start.isoformat()}",
            defaults={
                "subscription": subscription,
                "period_start": period_start,
                "period_end": _period_end(period_start),
                "subscription_amount": price.total,
                "masters_count": price.masters_count,
                "extra_masters_amount": price.extra_masters_amount,
                "fees_amount": 0,
                "total_amount": price.total,
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
    require_charges_enabled()  # DRF-2984: before any row or provider call
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
        _apply_pending_tariff(subscription, period_start=period_start)
        # DRF-2984: priced on the billable-masters snapshot taken now.
        price = period_price(subscription)
        invoice, created = BillingInvoice.objects.get_or_create(
            idempotency_key=f"charge:{subscription.id}:{period_start.isoformat()}",
            defaults={
                "subscription": subscription,
                "period_start": period_start,
                "period_end": _period_end(period_start),
                "subscription_amount": price.total,
                "masters_count": price.masters_count,
                "extra_masters_amount": price.extra_masters_amount,
                "fees_amount": 0,
                "total_amount": price.total,
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
    card_last4: str = "", card_brand: str = "",
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
            # Card read-model for C2 (provider data, may be blank).
            subscription.card_last4 = card_last4
            subscription.card_brand = card_brand
        became_active = subscription.status == SpecialistSubscription.Status.TRIAL
        subscription.status = SpecialistSubscription.Status.ACTIVE
        # Paying the debt lifts a soft freeze (DRF-2984).
        subscription.frozen_at = None
        subscription.frozen_reason = ""
        subscription.save()
        # DRF-2984: the events are part of the status change — same
        # transaction (the topics are registered; no best-effort anymore).
        if became_active:
            events.emit_subscription_activated(subscription)
        events.emit_subscription_renewed(
            subscription, amount=invoice.total_amount,
            masters_count=invoice.masters_count,
        )


def register_charge_failure(
    *, subscription: SpecialistSubscription, invoice: BillingInvoice,
    payment_row: BillingPayment | None = None, reason: str = "",
) -> None:
    """Dunning: retry T+1d, T+3d, then frozen (+ subscription.frozen)."""
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
            # DRF-2984 В-6: dunning exhausted → soft freeze (terminal until
            # the debt is paid). past_due stays for rows from before.
            subscription.next_retry_at = None
            subscription.status = SpecialistSubscription.Status.FROZEN
            subscription.frozen_at = timezone.now()
            subscription.frozen_reason = FROZEN_PAST_DUE_TIMEOUT
            subscription.save(
                update_fields=[
                    "failed_attempts", "next_retry_at", "status",
                    "frozen_at", "frozen_reason", "updated_at",
                ],
            )
            invoice.status = BillingInvoice.Status.FAILED
            invoice.save(update_fields=["status", "updated_at"])
            # DRF-2984 C3: the status is frozen, so the event is frozen —
            # past_due is no longer set by new code (В-6, variant 1).
            events.emit_subscription_frozen(subscription)


def retry_open_invoice(*, subscription: SpecialistSubscription, client=None) -> bool:
    """Re-attempt the latest open invoice (dunning retry task)."""
    require_charges_enabled()  # DRF-2984: before any row or provider call
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
    require_charges_enabled()  # DRF-2984: before any row or provider call
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
    require_charges_enabled()  # DRF-2984: before any row or provider call
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
                card_last4=info["card_last4"] if method_id else "",
                card_brand=info["card_brand"] if method_id else "",
            )
        elif event == "payment.canceled" and info["status"] == "canceled":
            register_charge_failure(
                subscription=subscription, invoice=invoice,
                payment_row=locked, reason="provider_canceled",
            )
        else:
            return "ignored"
    return "ok"


# --- DRF-2984 C3: the tenant-scoped account (contract §6) -------------------


class NoTenantAdmin(Exception):
    """The tenant has no active admin to be the payer of its account."""


class PriceMismatch(Exception):
    """The price the owner confirmed is not the current salon price."""


class AlreadySalon(Exception):
    """The cabinet is already a salon."""


def tariff_for_tenant(tenant) -> TariffPlan:
    """The plan follows the cabinet kind (Z8), never the request body."""
    from tenants.models import Tenant

    code = (
        TariffPlan.Code.SOLO if tenant.kind == Tenant.Kind.SOLO
        else TariffPlan.Code.SALON
    )
    return TariffPlan.objects.get(code=code, is_active=True)


def _tenant_payer(tenant):
    """The payer is the earliest active admin of the cabinet: the owner,
    or the solo master, who administers their own workspace."""
    from users.models import TenantUserRelationship

    rel = (
        TenantUserRelationship.objects
        .filter(
            tenant=tenant, role=TenantUserRelationship.Role.ADMIN,
            is_active=True,
        )
        .select_related("user")
        .order_by("granted_at")
        .first()
    )
    if rel is None:
        raise NoTenantAdmin(str(tenant.pk))
    return rel.user


def tenant_has_published_master(tenant) -> bool:
    """В-10: the trial starts once the cabinet is visible to clients."""
    from users.models import SpecialistProfile
    from users.sellable import sellable_q

    return SpecialistProfile.objects.filter(sellable_q(), tenant=tenant).exists()


def ensure_tenant_subscription(tenant, *, today=None):
    """Open the cabinet account; idempotent. Returns ``(subscription,
    created)``. The trial month starts now when the cabinet already has
    a master on sale, otherwise on the first publication
    (:func:`start_pending_trials`)."""
    today = today or timezone.localdate()
    existing = SpecialistSubscription.objects.filter(tenant=tenant).first()
    if existing is not None:
        return existing, False
    payer = _tenant_payer(tenant)
    try:
        with transaction.atomic():
            subscription = SpecialistSubscription.objects.create(
                user=payer, tenant=tenant, tariff=tariff_for_tenant(tenant),
                status=SpecialistSubscription.Status.TRIAL,
            )
    except IntegrityError:
        # Two first calls raced; the unique-per-tenant row is the answer.
        return SpecialistSubscription.objects.get(tenant=tenant), False
    if tenant_has_published_master(tenant):
        start_trial(subscription, started_on=today)
    return subscription, True


def start_pending_trials(*, today=None) -> int:
    """Daily: open the trial of every account whose cabinet got its first
    master on sale since (В-10). Publication happens in several places
    (moderation, admin actions), so a sweep catches them all."""
    today = today or timezone.localdate()
    pending = (
        SpecialistSubscription.objects
        .filter(
            status=SpecialistSubscription.Status.TRIAL,
            trial_ends_at__isnull=True,
            tenant__isnull=False,
        )
        .select_related("tenant", "tariff")
    )
    started = 0
    for subscription in pending:
        if tenant_has_published_master(subscription.tenant):
            start_trial(subscription, started_on=today)
            started += 1
    return started


def cancel_effective_on(subscription: SpecialistSubscription):
    """The account works until the end of what is paid (or of the trial)."""
    if subscription.current_period_end is not None:
        return subscription.current_period_end + timedelta(days=1)
    return timezone.localdate()


def cancel_subscription(subscription: SpecialistSubscription):
    """Stop auto-renewal; idempotent. Returns the effective date."""
    with transaction.atomic():
        locked = SpecialistSubscription.objects.select_for_update().get(
            pk=subscription.pk,
        )
        effective_on = cancel_effective_on(locked)
        if locked.status != SpecialistSubscription.Status.CANCELED:
            locked.status = SpecialistSubscription.Status.CANCELED
            locked.canceled_at = timezone.now()
            locked.next_retry_at = None
            locked.save(update_fields=[
                "status", "canceled_at", "next_retry_at", "updated_at",
            ])
            # No further auto-charges: the consent ends with the account.
            BillingConsent.objects.filter(
                user=locked.user, revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
            events.emit_subscription_canceled(locked, effective_at=effective_on)
    subscription.refresh_from_db()
    return effective_on


def convert_to_salon(tenant, *, confirmed_price, today=None):
    """Solo cabinet becomes a salon (В-2), confirmed with the price.

    The kind changes now, so a second master can be provisioned at once;
    the price changes from the next period, so the current one is never
    charged twice. Idempotent: a repeat answers with the pending change.
    Raises PriceMismatch / AlreadySalon.
    """
    from tenants.models import Tenant

    today = today or timezone.localdate()
    salon = TariffPlan.objects.get(code=TariffPlan.Code.SALON, is_active=True)
    if quantize_money(confirmed_price) != quantize_money(salon.price):
        raise PriceMismatch(f"{salon.price:.2f}")
    with transaction.atomic():
        locked = Tenant.all_objects.select_for_update().get(pk=tenant.pk)
        subscription = (
            SpecialistSubscription.objects.select_for_update()
            .filter(tenant=locked).first()
        )
        if locked.kind == Tenant.Kind.SALON:
            if (
                subscription is not None
                and subscription.pending_tariff_id == salon.pk
            ):
                return subscription  # repeat of a confirmed conversion
            raise AlreadySalon(str(locked.pk))
        locked.kind = Tenant.Kind.SALON
        locked.save(update_fields=["kind"])
        if subscription is None:
            return None
        subscription.pending_tariff = salon
        subscription.pending_tariff_from = (
            subscription.current_period_end + timedelta(days=1)
            if subscription.current_period_end is not None else today
        )
        subscription.save(update_fields=[
            "pending_tariff", "pending_tariff_from", "updated_at",
        ])
        events.emit_subscription_plan_changed(subscription)
    return subscription


def _apply_pending_tariff(subscription: SpecialistSubscription, *, period_start):
    """Switch to the confirmed plan when its period begins.

    Read under the row lock, not from the caller's instance: the owner may
    confirm the conversion after the sweep loaded the row. Call inside a
    transaction.
    """
    locked = (
        # No select_related: Postgres rejects FOR UPDATE on the nullable
        # side of an outer join (pending_tariff is nullable).
        SpecialistSubscription.objects.select_for_update().get(pk=subscription.pk)
    )
    if (
        locked.pending_tariff_id is None
        or locked.pending_tariff_from is None
        or period_start < locked.pending_tariff_from
    ):
        return
    locked.tariff = locked.pending_tariff
    locked.pending_tariff = None
    locked.pending_tariff_from = None
    locked.save(update_fields=[
        "tariff", "pending_tariff", "pending_tariff_from", "updated_at",
    ])
    subscription.tariff = locked.tariff
    subscription.pending_tariff = None
    subscription.pending_tariff_from = None
