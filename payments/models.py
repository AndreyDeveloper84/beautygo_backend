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


class UserPaymentMethod(models.Model):
    """Saved client card (C7.2) — opt-in binding, never a payment side
    effect.

    Consent boundary (PILOT_CONTRACTS §7.5, AYLA-DEC-0011): a row is
    created ONLY when the provider confirms ``payment_method.saved ==
    true`` after a user-initiated binding flow with explicit consent
    (``consent_version`` + ``consented_at``). A saved method may be used
    solely for user-initiated charges — no client auto-charges in the
    pilot (AYLA-DEC-0001). ``revoked_at`` set = the method is dead:
    ``chargeable()`` returns False and any charge path must refuse it
    (delete → charge forbidden).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='payment_methods',
    )
    # Provider-side token of the saved method (YooKassa payment_method.id).
    payment_method_id = models.CharField(max_length=200, unique=True)
    last4 = models.CharField(max_length=4)
    brand = models.CharField(max_length=32)
    # Explicit consent proof: which text version the user accepted and
    # when. Set at binding time from the setup call's metadata.
    consent_version = models.CharField(max_length=64)
    consented_at = models.DateTimeField()
    # NULL = active. Set on user revoke (C7.2 delete) — 152-ФЗ-adjacent
    # right; never hard-delete the row (audit trail of the consent).
    revoked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Способ оплаты пользователя'
        verbose_name_plural = 'Способы оплаты пользователей'
        indexes = [
            models.Index(fields=['user'], name='upm_user_idx'),
        ]

    def chargeable(self) -> bool:
        """A revoked method must never be charged (C7.2 boundary)."""
        return self.revoked_at is None

    def revoke(self) -> None:
        """User-initiated revoke — after this, charges are forbidden."""
        from django.utils import timezone
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=['revoked_at', 'updated_at'])

    def __str__(self) -> str:
        return f"{self.brand} ···· {self.last4} (user {self.user_id})"
