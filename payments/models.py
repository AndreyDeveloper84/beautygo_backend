"""Payment model — canonical home in the `payments` app.

Moved from `appointments/models.py` per issue #426 (Bucket 5). The
underlying SQL table was renamed `appointments_payment → payments_payment`
in #492 (`payments/migrations/0002_rename_table.py`). Django now uses
its default `<app_label>_<modelname>` naming — no `db_table` override
needed (and future maintainers shouldn't add one — Django's E028 catches
duplicate db_table values across models).

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

    class CaptureState(models.TextChoices):
        """Two-stage capture lifecycle (D9, C3 payout-preview vocabulary).

        NONE — single-stage or not yet held. The payout preview (C3)
        counts exactly SCHEDULED + CAPTURED_PENDING_SETTLEMENT.
        """
        NONE = "", "Не применимо"
        SCHEDULED = "scheduled", "Холд есть, capture запланирован"
        CAPTURED_PENDING_SETTLEMENT = (
            "captured_pending_settlement", "Capture выполнен, ждёт выплаты ЮKassa"
        )
        SETTLED = "settled", "Выплачено мастеру"
        CAPTURE_FAILED = "capture_failed", "Capture не удался"
        CANCELED = "canceled", "Холд отменён"
        REFUNDED = "refunded", "Возвращено клиенту"

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

    # --- Two-stage capture lifecycle (D9) --------------------------------
    capture_state = models.CharField(
        max_length=32,
        choices=CaptureState.choices,
        default=CaptureState.NONE,
        db_index=True,
    )
    # When the capture task is planned (complete() + CAPTURE_DELAY_HOURS,
    # clamped to expires_at − safety buffer). Null until scheduled.
    capture_scheduled_for = models.DateTimeField(null=True, blank=True)
    # Hold deadline reported by YooKassa — capture must happen before
    # this minus CAPTURE_SAFETY_BUFFER_MINUTES, else the hold auto-cancels
    # (expired_on_capture). Null until the waiting_for_capture webhook.
    yookassa_expires_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Default db_table now resolves to 'payments_payment' via
        # app_label='payments' + model name 'payment'. The actual SQL
        # rename was applied in payments/migrations/0002_rename_table.py
        # (#492).
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
