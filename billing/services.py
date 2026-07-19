"""Billing service layer — C1 eligibility, fee accrual, C2 status.

Contracts (PILOT_CONTRACTS_2026-08-15 v1.3.0):

- **C1** ``can_accept_booking(specialist_id, tenant_id)`` — may the
  specialist accept a NEW booking. Python call (not HTTP); W1's adapter
  imports it from here (AMD-003). Fail-open with Sentry, never raises.
- **AYLA-DEC-0010 + AMD-009** — BookingFee accrues on completion exactly
  once per appointment and only when NO online payment in
  {authorized, paid} exists (online-paid bookings are charged via the
  capture split in `payments/`).
- **C2 + AMD-005** — billing status payload keyed by Ayla User UUID.

Money (§1): all wire amounts are 2dp Decimal strings, ROUND_HALF_UP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import sentry_sdk
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from billing import events
from billing.models import (
    BookingFee,
    SpecialistSubscription,
    compute_booking_fee,
    quantize_money,
)

if TYPE_CHECKING:  # pragma: no cover
    from appointments.models import Appointment
    from users.models import SpecialistProfile

logger = logging.getLogger(__name__)

REASON_SUBSCRIPTION_PAST_DUE = "SUBSCRIPTION_PAST_DUE"


# ---------------------------------------------------------------------------
# C1 — booking eligibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """C1 contract result. Invariants are enforced at construction."""

    ok: bool
    reason: Literal["SUBSCRIPTION_PAST_DUE"] | None = None

    def __post_init__(self) -> None:
        if self.ok and self.reason is not None:
            raise ValueError("ok=True requires reason=None")
        if not self.ok and self.reason != REASON_SUBSCRIPTION_PAST_DUE:
            raise ValueError("ok=False requires reason=SUBSCRIPTION_PAST_DUE")


def resolve_billing_account(
    *, user_id: UUID, tenant_id: UUID | None,
) -> SpecialistSubscription | None:
    """C1 resolution: salon subscription when the tenant has one
    (it then governs ALL masters of that salon), else the specialist's
    personal subscription. AMD-005: ``user_id`` is the Ayla User UUID.
    """
    if tenant_id is not None:
        salon = SpecialistSubscription.objects.filter(tenant_id=tenant_id).first()
        if salon is not None:
            return salon
    return SpecialistSubscription.objects.filter(
        user_id=user_id, tenant__isnull=True,
    ).first()


def can_accept_booking(
    specialist_id: UUID, tenant_id: UUID | None,
) -> EligibilityResult:
    """C1: may this specialist accept a NEW booking right now.

    Only ``past_due`` blocks. Fail-open per contract: missing data or
    technical errors return ``ok=True`` and alert Sentry WITHOUT any
    personal or payment data. Existing bookings, reschedule, cancel and
    completion are never gated here (consumer-side rule, C1).
    """
    try:
        subscription = resolve_billing_account(
            user_id=specialist_id, tenant_id=tenant_id,
        )
        if subscription is None:
            logger.warning("billing.eligibility.account_not_found")
            sentry_sdk.capture_message(
                "billing.eligibility.account_not_found", level="warning",
            )
            return EligibilityResult(ok=True)
        if subscription.status == SpecialistSubscription.Status.PAST_DUE:
            return EligibilityResult(ok=False, reason=REASON_SUBSCRIPTION_PAST_DUE)
        return EligibilityResult(ok=True)
    except Exception as exc:  # fail-open (C1) — never raise to the booking path
        logger.exception("billing.eligibility.error")
        sentry_sdk.capture_exception(exc)
        return EligibilityResult(ok=True)


# ---------------------------------------------------------------------------
# BookingFee accrual (AYLA-DEC-0010 / AMD-009)
# ---------------------------------------------------------------------------


def has_online_payment(appointment: "Appointment") -> bool:
    """AMD-009 predicate: an online payment EXISTS for this appointment
    iff a Payment in {authorized, paid, refunded, partially_refunded} is
    attached. failed/pending are abandoned attempts, not online payment.
    Refunded states still count: the money DID flow online (split was
    taken at capture and the refund reverses it) — a paid-then-refunded
    booking must NOT spawn a BookingFee (AMD-009)."""
    from payments.models import Payment

    return appointment.payments.filter(
        status__in=(
            Payment.Status.AUTHORIZED,
            Payment.Status.PAID,
            Payment.Status.REFUNDED,
            Payment.Status.PARTIALLY_REFUNDED,
        ),
    ).exists()


def accrue_booking_fee(appointment: "Appointment") -> BookingFee | None:
    """Accrue the 90₽ fee for a completed appointment — at most once.

    Returns the fee row (existing or new), or None when no fee is due
    (online-paid) or cannot be accrued (no billing account — a
    reconciliation incident per AYLA-DEC-0010, alerted to Sentry).
    Event emission is best-effort: the fee row is the business fact.
    """
    if has_online_payment(appointment):
        return None
    subscription = resolve_billing_account(
        user_id=appointment.specialist.user_id,
        tenant_id=appointment.tenant_id,
    )
    if subscription is None:
        logger.error(
            "billing.fee.account_not_found appointment_id=%s", appointment.pk,
        )
        sentry_sdk.capture_message(
            "billing.fee.account_not_found", level="error",
        )
        return None
    period_start = timezone.localtime(appointment.end_datetime).date().replace(day=1)
    with transaction.atomic():
        fee, created = BookingFee.objects.get_or_create(
            appointment=appointment,
            defaults={
                "subscription": subscription,
                "amount": compute_booking_fee(appointment.price),
                "period_start": period_start,
            },
        )
    if created:
        try:
            events.emit_fee_charged(fee)
        except Exception as exc:  # topics may be unregistered pre-W1-patch
            logger.exception("billing.fee.event_emit_failed fee_id=%s", fee.pk)
            sentry_sdk.capture_exception(exc)
    return fee


# ---------------------------------------------------------------------------
# C2 — billing status payload
# ---------------------------------------------------------------------------


def build_billing_status(specialist: "SpecialistProfile") -> dict:
    """C2 response ``data`` block for the specialist's billing account.

    No account → the "none" shape with nulls and zeroes (C2: an empty
    selection is always 200, never 404). Money is serialized as 2dp
    strings (§1); dates ISO 8601.
    """
    specialist_id = str(specialist.user_id)
    subscription = resolve_billing_account(
        user_id=specialist.user_id, tenant_id=specialist.tenant_id,
    )
    if subscription is None:
        return {
            "specialist_id": specialist_id,
            "subscription": {
                "status": "none",
                "tariff": None,
                "current_period_end": None,
                "next_charge": None,
            },
            "fees": {"pending_total": "0.00", "pending_count": 0},
            "last_invoice": None,
        }

    pending = BookingFee.objects.filter(
        subscription=subscription, status=BookingFee.Status.PENDING,
    ).aggregate(total=Sum("amount"), count=Count("id"))
    fees_amount = quantize_money(pending["total"] or 0)
    subscription_amount = quantize_money(subscription.tariff.price)

    # next_charge: what the upcoming monthly charge will collect. Null
    # for canceled accounts (nothing more is charged). The charge runs
    # the day after the paid period ends (renewal, charge-in-advance).
    if subscription.status == SpecialistSubscription.Status.CANCELED:
        next_charge = None
    else:
        next_date = (
            subscription.current_period_end + timedelta(days=1)
            if subscription.current_period_end else None
        )
        next_charge = {
            "subscription_amount": f"{subscription_amount:.2f}",
            "fees_amount": f"{fees_amount:.2f}",
            "total_amount": f"{subscription_amount + fees_amount:.2f}",
            "date": next_date.isoformat() if next_date else None,
        }

    last_invoice = subscription.invoices.order_by("-created_at").first()
    return {
        "specialist_id": specialist_id,
        "subscription": {
            "status": subscription.status,
            "tariff": subscription.tariff.code,
            "current_period_end": (
                subscription.current_period_end.isoformat()
                if subscription.current_period_end else None
            ),
            "next_charge": next_charge,
        },
        "fees": {
            "pending_total": f"{fees_amount:.2f}",
            "pending_count": pending["count"],
        },
        "last_invoice": (
            {
                "id": str(last_invoice.id),
                "amount": f"{last_invoice.total_amount:.2f}",
                "status": last_invoice.status,
                "paid_at": (
                    last_invoice.paid_at.isoformat() if last_invoice.paid_at else None
                ),
            }
            if last_invoice else None
        ),
    }
