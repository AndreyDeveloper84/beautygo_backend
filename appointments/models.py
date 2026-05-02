"""Appointment model — core booking entity + booking engine models."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from .domain.value_objects import (
    ACTIVE_BOOKING_STATUSES,
    BookingStateMachine,
    BookingStatus,
)


class Appointment(models.Model):
    """A booking between a client and a specialist for a specific service."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает подтверждения"
        AWAITING_PAYMENT = "awaiting_payment", "Ожидает оплаты"
        CONFIRMED = "confirmed", "Подтверждена"
        IN_PROGRESS = "in_progress", "В процессе"
        COMPLETED = "completed", "Завершена"
        CANCELLED = "cancelled", "Отменена"
        NO_SHOW = "no_show", "Клиент не пришёл"

    # Terminal statuses — no further transitions allowed
    TERMINAL_STATUSES = {Status.COMPLETED, Status.CANCELLED, Status.NO_SHOW}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='appointments_as_client',
    )
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.PROTECT,
        related_name='appointments',
    )
    # tenant FK — DRF-242.3. Denormalized from specialist.tenant for query
    # performance: scoping middleware filters bookings by tenant before
    # any join. null=True until 242.4 backfill.
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='appointments',
    )
    service = models.ForeignKey(
        'services.Service',
        on_delete=models.PROTECT,
        related_name='appointments',
    )

    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    # --- Idempotency ---
    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True,
        help_text="Client-provided UUID for duplicate booking prevention",
    )

    # --- Snapshot fields (immutable record of what was agreed) ---
    price = models.DecimalField(max_digits=10, decimal_places=2)
    snapshot_service_name = models.CharField(max_length=200, default="")
    snapshot_duration_minutes = models.PositiveSmallIntegerField(default=0)
    snapshot_price = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    snapshot_commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
    )
    snapshot_specialist_income = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    snapshot_platform_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    snapshot_timezone = models.CharField(
        max_length=50, default="Europe/Moscow",
    )

    # --- Visit tracking ---
    is_first_visit = models.BooleanField(default=True)

    # --- Notes & cancellation ---
    notes = models.TextField(blank=True)
    cancellation_reason = models.CharField(
        max_length=500, blank=True, default="",
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cancellations',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_datetime']
        verbose_name = 'Запись'
        verbose_name_plural = 'Записи'
        indexes = [
            models.Index(
                fields=['specialist', 'start_datetime', 'end_datetime'],
                name='appt_specialist_time_idx',
            ),
            models.Index(
                fields=['specialist', 'status'],
                name='appt_specialist_status_idx',
            ),
            models.Index(
                fields=['client', 'status'],
                name='appt_client_status_idx',
            ),
            models.Index(
                fields=['status', 'start_datetime'],
                name='appt_status_time_idx',
            ),
        ]

    def clean(self) -> None:
        """Validate appointment constraints."""
        if self.end_datetime and self.start_datetime:
            if self.end_datetime <= self.start_datetime:
                raise ValidationError(
                    {'end_datetime': 'End datetime must be after start datetime.'}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"{self.client} -> {self.specialist.display_name} "
            f"[{self.get_status_display()}] {self.start_datetime:%Y-%m-%d %H:%M}"
        )

    # --- State machine helpers ---

    @property
    def booking_status(self) -> BookingStatus:
        """Convert DB status string to domain BookingStatus enum."""
        return BookingStatus(self.status)

    @property
    def is_active(self) -> bool:
        """True if this appointment holds a slot (blocks future bookings)."""
        try:
            return self.booking_status in ACTIVE_BOOKING_STATUSES
        except ValueError:
            return False

    @property
    def is_terminal(self) -> bool:
        try:
            return BookingStateMachine.is_terminal(self.booking_status)
        except ValueError:
            return self.status in {s.value for s in self.TERMINAL_STATUSES}

    def can_cancel(self) -> bool:
        try:
            return BookingStateMachine.can_transition(
                self.booking_status, BookingStatus.CANCELLED,
            )
        except ValueError:
            return False

    def can_complete(self) -> bool:
        try:
            return BookingStateMachine.can_transition(
                self.booking_status, BookingStatus.COMPLETED,
            )
        except ValueError:
            return False

    def cancel(self, cancelled_by, reason: str = "") -> None:
        """Cancel the appointment via state machine."""
        if not self.can_cancel():
            raise ValidationError(
                f"Cannot cancel appointment with status '{self.status}'."
            )
        self.status = self.Status.CANCELLED
        self.cancellation_reason = reason
        self.cancelled_by = cancelled_by
        self.save(update_fields=[
            'status', 'cancellation_reason', 'cancelled_by', 'updated_at',
        ])

    def complete(self) -> None:
        """Mark appointment as completed via state machine."""
        if not self.can_complete():
            raise ValidationError(
                f"Cannot complete appointment with status '{self.status}'."
            )
        self.status = self.Status.COMPLETED
        self.save(update_fields=['status', 'updated_at'])

    @property
    def duration_minutes(self) -> int:
        """Computed duration from start/end."""
        if self.start_datetime and self.end_datetime:
            return int(
                (self.end_datetime - self.start_datetime).total_seconds() / 60
            )
        return 0


# ---------------------------------------------------------------------------
# SpecialistWorkingHours — weekly schedule template
# ---------------------------------------------------------------------------

class SpecialistWorkingHours(models.Model):
    """Weekly working hours template for a specialist."""

    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Понедельник"
        TUESDAY = 1, "Вторник"
        WEDNESDAY = 2, "Среда"
        THURSDAY = 3, "Четверг"
        FRIDAY = 4, "Пятница"
        SATURDAY = 5, "Суббота"
        SUNDAY = 6, "Воскресенье"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='working_hours',
    )
    day_of_week = models.PositiveSmallIntegerField(
        choices=DayOfWeek.choices,
    )
    is_working_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    break_start = models.TimeField(null=True, blank=True)
    break_end = models.TimeField(null=True, blank=True)

    class Meta:
        unique_together = [('specialist', 'day_of_week')]
        ordering = ['day_of_week']
        verbose_name = 'Рабочие часы'
        verbose_name_plural = 'Рабочие часы'
        indexes = [
            models.Index(
                fields=['specialist', 'day_of_week'],
                name='working_hours_lookup_idx',
            ),
        ]

    def __str__(self) -> str:
        day_name = self.get_day_of_week_display()
        if not self.is_working_day:
            return f"{self.specialist} — {day_name}: выходной"
        return (
            f"{self.specialist} — {day_name}: "
            f"{self.start_time:%H:%M}-{self.end_time:%H:%M}"
        )


# ---------------------------------------------------------------------------
# SpecialistTimeOff — vacation, sick days, personal blocks
# ---------------------------------------------------------------------------

class SpecialistTimeOff(models.Model):
    """Explicit time-off blocks for a specialist (UTC datetimes)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='time_offs',
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-start_at']
        verbose_name = 'Выходной/отпуск'
        verbose_name_plural = 'Выходные/отпуска'
        indexes = [
            models.Index(
                fields=['specialist', 'start_at', 'end_at'],
                name='time_off_specialist_range_idx',
            ),
        ]

    def clean(self) -> None:
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValidationError(
                {'end_at': 'End must be after start.'}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.specialist} — {self.start_at:%Y-%m-%d} - {self.end_at:%Y-%m-%d}"


# ---------------------------------------------------------------------------
# Payment — payment records linked to appointments
# ---------------------------------------------------------------------------

class Payment(models.Model):
    """Payment record for an appointment."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        AUTHORIZED = "authorized", "Авторизован"
        PAID = "paid", "Оплачен"
        FAILED = "failed", "Ошибка"
        REFUNDED = "refunded", "Возвращён"
        PARTIALLY_REFUNDED = "partially_refunded", "Частичный возврат"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    appointment = models.ForeignKey(
        Appointment,
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


# ---------------------------------------------------------------------------
# OutboxEvent — transactional outbox for guaranteed event delivery
# ---------------------------------------------------------------------------

class OutboxEvent(models.Model):
    """
    Transactional outbox: events written atomically with domain changes.

    Workers pick up unprocessed events (processed_at IS NULL) and handle them.
    This guarantees no events are lost even if the app crashes after DB commit.
    """

    class Topic(models.TextChoices):
        BOOKING_CREATED = "booking.created", "Запись создана"
        BOOKING_CONFIRMED = "booking.confirmed", "Запись подтверждена"
        BOOKING_CANCELLED = "booking.cancelled", "Запись отменена"
        BOOKING_RESCHEDULED = "booking.rescheduled", "Запись перенесена"
        BOOKING_COMPLETED = "booking.completed", "Запись завершена"
        BOOKING_NO_SHOW = "booking.no_show", "Клиент не пришёл"
        PAYMENT_CONFIRMED = "payment.confirmed", "Оплата подтверждена"
        PAYMENT_REFUNDED = "payment.refunded", "Возврат оплаты"
        CACHE_INVALIDATE_SLOTS = "cache.invalidate_slots", "Инвалидация кеша слотов"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=50, choices=Topic.choices)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Outbox Event'
        verbose_name_plural = 'Outbox Events'

    def __str__(self) -> str:
        status = "processed" if self.processed_at else "pending"
        return f"{self.topic} ({status}) — {self.created_at:%Y-%m-%d %H:%M}"
