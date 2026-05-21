"""Payment model — canonical home in the `payments` app.

Moved from `appointments/models.py` per issue #426 (Bucket 5). The
`db_table` is pinned to the legacy name `appointments_payment` for this
PR so no data needs to move; a follow-up PR (#426 step 2) renames the
table to the conventional `payments_payment` via `AlterModelTable`.

ADR-0009 §Domain ownership matrix: payments are an Ayla-canonical domain
(`payments/` app owns the YooKassa lifecycle). The split-from-appointments
refactor closes the bounded-context leak the audit flagged at §3.5.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Payment(models.Model):
    """Payment record for an appointment.

    Lifecycle: PENDING → AUTHORIZED → PAID → (REFUNDED | PARTIALLY_REFUNDED).
    The YooKassa webhook flow (see `payments/views.py`) drives state
    transitions and uses `last_webhook_event_id` for idempotency.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        AUTHORIZED = "authorized", "Авторизован"
        PAID = "paid", "Оплачен"
        FAILED = "failed", "Ошибка"
        REFUNDED = "refunded", "Возвращён"
        PARTIALLY_REFUNDED = "partially_refunded", "Частичный возврат"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment = models.ForeignKey(
        # String FK target avoids a circular import between
        # appointments.models and payments.models — appointments.Appointment
        # imports nothing from payments, but the reverse manager
        # `appointment.payments` is resolved lazily at runtime regardless.
        'appointments.Appointment',
        on_delete=models.PROTECT,
        related_name='payments',
    )

    # Financial fields
    amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    status = models.CharField(
        max_length=25,
        choices=Status.choices,
        default=Status.PENDING,
    )
    specialist_income = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    platform_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    refunded_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )

    # Provider integration
    provider = models.CharField(max_length=50, blank=True, default="")
    provider_payment_id = models.CharField(
        max_length=200, blank=True, default="", db_index=True,
    )
    provider_client_secret = models.CharField(
        max_length=500, blank=True, default="",
    )

    # Webhook idempotency
    last_webhook_event_id = models.CharField(
        max_length=200, blank=True, default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # db_table pinned to the legacy name so PR 1 is state-only (no
        # data move). PR 2 of #426 will drop this and let Django default
        # to `payments_payment` via `AlterModelTable`.
        db_table = 'appointments_payment'
        verbose_name = 'Платёж'
        verbose_name_plural = 'Платежи'
        indexes = [
            models.Index(fields=['appointment'], name='payment_appointment_idx'),
        ]

    @property
    def net_amount(self):
        """Amount after refunds."""
        return self.amount - self.refunded_amount

    def __str__(self) -> str:
        return f"Payment {self.id} — {self.amount} ({self.status})"
