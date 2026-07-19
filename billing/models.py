"""Billing models — W2 stream (Ayla Billing & Legal, pilot 2026-08-15).

Owns the money flow specialist → platform (AYLA-DEC-0001):
subscription 690₽ solo / 990₽ salon (up to 3 masters) + 90₽ fee per
completed booking. The reverse flow (client → specialist split) lives in
`payments/` and belongs to W1 — do not duplicate it here.

Contracts implemented (PILOT_CONTRACTS_2026-08-15 v1.3.0):
- C1/C2/C4 + AMD-005: the master key everywhere is the Ayla **User UUID**
  (NOT SpecialistProfile.id) — subscription owner is a User FK; resolving
  user → SpecialistProfile happens in billing/services.py.
- C4 invariant (AYLA-DEC-0010): at most ONE BookingFee per appointment —
  enforced by the OneToOneField on `appointment` (UNIQUE(appointment_id)).
- AMD-004: fee edge rule — `compute_booking_fee(price) = min(90.00, price)`,
  clamped to >= 0 (negative amounts are forbidden by §1).
- AMD-009: BookingFee accrues only when NO Payment in {authorized, paid}
  exists for the appointment (predicate lives in billing/services.py).
- B-6: BillingConsent stores the offer *version* string; the offer text
  itself is an external legal task (TODO(legal)).

Money contract (§1): Decimal fields (10,2); serialization to 2dp strings
with ROUND_HALF_UP happens in the service layer.
"""
from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

# AYLA-DEC-0001: flat platform fee per successful booking.
BOOKING_FEE_AMOUNT = Decimal("90.00")

_MONEY = dict(max_digits=10, decimal_places=2)
_ZERO = Decimal("0.00")


def quantize_money(value: Decimal) -> Decimal:
    """Round to the §1 money shape: 2 decimal places, ROUND_HALF_UP."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_booking_fee(price: Decimal) -> Decimal:
    """AMD-004 edge rule: fee = min(90.00, price), never negative.

    A service cheaper than the fee yields fee == price (the specialist
    keeps nothing on the platform side), a free service yields 0.00.
    """
    return max(_ZERO, min(BOOKING_FEE_AMOUNT, quantize_money(price)))


class TariffPlan(models.Model):
    """Subscription price list (AYLA-DEC-0001). Seeded by data migration."""

    class Code(models.TextChoices):
        SOLO = "solo", "Самостоятельный мастер"
        SALON = "salon", "Салон (до 3 мастеров)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=20, choices=Code.choices, unique=True)
    name = models.CharField(max_length=120)
    price = models.DecimalField(**_MONEY, validators=[MinValueValidator(_ZERO)])
    max_masters = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Тарифный план'
        verbose_name_plural = 'Тарифные планы'

    def __str__(self) -> str:
        return f"{self.code} — {self.price}₽/мес"


class SpecialistSubscription(models.Model):
    """Billing account of a solo master (user) or a salon (tenant).

    Exactly one owner: `user` XOR `tenant` (CheckConstraint). One row per
    owner — the subscription is updated in place, not versioned (pilot).
    """

    class Status(models.TextChoices):
        TRIAL = "trial", "Пробный период"
        ACTIVE = "active", "Активна"
        PAST_DUE = "past_due", "Просрочена"
        CANCELED = "canceled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # AMD-005: keyed by Ayla User UUID. NULL iff this is a salon account.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='billing_subscriptions',
    )
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='billing_subscriptions',
    )
    tariff = models.ForeignKey(
        TariffPlan,
        on_delete=models.PROTECT,
        related_name='subscriptions',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.TRIAL,
    )
    current_period_start = models.DateField(null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)

    # YooKassa saved card (D7 recurrent). Empty until the first payment
    # with save_payment_method:true succeeds (webhook payment.succeeded).
    payment_method_id = models.CharField(max_length=200, blank=True, default="")
    payment_method_saved_at = models.DateTimeField(null=True, blank=True)

    # Dunning (wave 2): fail → retry T+1d, T+3d → past_due.
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Подписка специалиста'
        verbose_name_plural = 'Подписки специалистов'
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, tenant__isnull=True)
                    | models.Q(user__isnull=True, tenant__isnull=False)
                ),
                name='billing_subscription_exactly_one_owner',
            ),
            models.UniqueConstraint(
                fields=['user'],
                condition=models.Q(user__isnull=False),
                name='billing_subscription_unique_user',
            ),
            models.UniqueConstraint(
                fields=['tenant'],
                condition=models.Q(tenant__isnull=False),
                name='billing_subscription_unique_tenant',
            ),
        ]

    def __str__(self) -> str:
        owner = f"user={self.user_id}" if self.user_id else f"tenant={self.tenant_id}"
        return f"Subscription({owner}, {self.tariff_id}, {self.status})"


class BookingFee(models.Model):
    """90₽ platform fee for one completed booking (offline-paid only).

    C4 business invariant (AYLA-DEC-0010): ONE appointment → AT MOST one
    BookingFee — the OneToOneField IS the UNIQUE(appointment_id) guard.
    Online-paid bookings never get a row here: their 90₽ is withheld by
    the capture split in `payments/` (AMD-009 predicate in services.py).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Начислен"
        INVOICED = "invoiced", "Включён в инвойс"
        CHARGED = "charged", "Оплачен"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment = models.OneToOneField(
        'appointments.Appointment',
        on_delete=models.PROTECT,
        related_name='booking_fee',
    )
    subscription = models.ForeignKey(
        SpecialistSubscription,
        on_delete=models.PROTECT,
        related_name='booking_fees',
    )
    amount = models.DecimalField(**_MONEY, validators=[MinValueValidator(_ZERO)])
    # Billing period (first day of the completion month) — C4 payload `period`.
    period_start = models.DateField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    invoice = models.ForeignKey(
        'BillingInvoice',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='booking_fees',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Комиссия за запись'
        verbose_name_plural = 'Комиссии за записи'
        indexes = [
            models.Index(
                fields=['subscription', 'period_start', 'status'],
                name='bookingfee_charge_idx',
            ),
        ]

    def __str__(self) -> str:
        return f"BookingFee {self.appointment_id} — {self.amount} ({self.status})"


class BillingInvoice(models.Model):
    """Monthly charge: subscription price + BookingFee sum for the period."""

    class Status(models.TextChoices):
        OPEN = "open", "Выставлен"
        PAID = "paid", "Оплачен"
        FAILED = "failed", "Ошибка оплаты"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        SpecialistSubscription,
        on_delete=models.PROTECT,
        related_name='invoices',
    )
    period_start = models.DateField()
    period_end = models.DateField()
    subscription_amount = models.DecimalField(**_MONEY, default=_ZERO)
    fees_amount = models.DecimalField(**_MONEY, default=_ZERO)
    total_amount = models.DecimalField(**_MONEY, default=_ZERO)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN,
    )
    # Idempotent monthly charge: one invoice per subscription+period.
    idempotency_key = models.CharField(max_length=100, unique=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Инвойс'
        verbose_name_plural = 'Инвойсы'

    def __str__(self) -> str:
        return f"Invoice {self.id} — {self.total_amount} ({self.status})"


class BillingPayment(models.Model):
    """YooKassa payment attempt for an invoice (or the card-binding charge).

    `setup` — first master payment with save_payment_method:true (D7);
    `recurrent` — monthly auto-charge via the saved payment_method_id.
    Retries create a new row each (mirrors payments.Payment 1:N style).
    """

    class Kind(models.TextChoices):
        SETUP = "setup", "Привязка карты"
        RECURRENT = "recurrent", "Автосписание"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        SUCCEEDED = "succeeded", "Успешен"
        FAILED = "failed", "Ошибка"
        CANCELED = "canceled", "Отменён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(
        BillingInvoice,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='payments',
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(**_MONEY, validators=[MinValueValidator(_ZERO)])
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    idempotency_key = models.CharField(max_length=100, unique=True)
    provider_payment_id = models.CharField(
        max_length=200, blank=True, default="", db_index=True,
    )
    confirmation_url = models.CharField(max_length=500, blank=True, default="")
    failure_reason = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Платёж биллинга'
        verbose_name_plural = 'Платежи биллинга'

    def __str__(self) -> str:
        return f"BillingPayment {self.id} — {self.amount} ({self.kind}/{self.status})"


class BillingConsent(models.Model):
    """Consent to recurrent auto-charge (AYLA-DEC-0007, 152-ФЗ paper trail).

    Records WHO accepted WHICH offer version and WHEN. The offer text
    itself is versioned externally — TODO(legal): B-6, text due week 2.
    One active (non-revoked) consent per user.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='billing_consents',
    )
    document_version = models.CharField(max_length=40)
    given_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Согласие на автоплатёж'
        verbose_name_plural = 'Согласия на автоплатёж'
        constraints = [
            models.UniqueConstraint(
                fields=['user'],
                condition=models.Q(revoked_at__isnull=True),
                name='billing_consent_single_active_per_user',
            ),
        ]

    def __str__(self) -> str:
        state = "revoked" if self.revoked_at else "active"
        return f"Consent(user={self.user_id}, v{self.document_version}, {state})"
