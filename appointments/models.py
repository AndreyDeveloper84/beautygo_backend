"""Appointment model — core booking entity + booking engine models."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

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
    # any join. null=False post-#590 — migration 0009 enforces this at
    # the schema level via AlterField + CheckConstraint, closing the
    # accidental-NULL window that backfill (#568) opened at the data
    # layer. CreateBookingService stamps tenant_id=specialist.tenant_id
    # at construction time (#520); admin/raw inserts now hit the
    # constraint instead of silently landing in the gap.
    tenant = models.ForeignKey(
        'tenants.Tenant',
        on_delete=models.PROTECT,
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
    # Set by complete() — the visit-completion timestamp. Drives the
    # payout preview (C3 items[].completed_at) and reconciliation of
    # stuck captures (D9). Null until completed.
    completed_at = models.DateTimeField(null=True, blank=True)

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
            # Tenant-scoped composite indexes — DRF-242.7. Marketplace
            # reporting filters by tenant before status / time window.
            models.Index(
                fields=['tenant', 'status'],
                name='appt_tenant_status_idx',
            ),
            models.Index(
                fields=['tenant', '-start_datetime'],
                name='appt_tenant_starttime_idx',
            ),
        ]
        constraints = [
            # #590: belt-and-suspenders for null=False on `tenant`.
            # MUST be declared here so Django's autodetector sees the
            # constraint as part of the model state, not just a DB
            # artifact. Without this declaration `makemigrations`
            # would generate a phantom `RemoveConstraint` migration
            # on every run — silently re-opening the gap the
            # 0009 AddConstraint closed.
            models.CheckConstraint(
                check=models.Q(tenant__isnull=False),
                name='appt_tenant_not_null',
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

    def can_mark_no_show(self) -> bool:
        try:
            return BookingStateMachine.can_transition(
                self.booking_status, BookingStatus.NO_SHOW,
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
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def mark_no_show(self) -> None:
        """Mark client as no-show via state machine.

        Triggered by the specialist after the booking time elapsed
        without the client arriving. Unlike ``cancel``, this preserves
        the "specialist took the slot" signal — important for revenue
        loss tracking, customer reliability scoring, and any future
        automated reschedule offer (#511).
        """
        if not self.can_mark_no_show():
            raise ValidationError(
                f"Cannot mark no-show on appointment with status "
                f"'{self.status}'."
            )
        self.status = self.Status.NO_SHOW
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


# Payment used to live here. Moved to `payments.models.Payment` in
# issue #426 (Phase 0 Bucket 5). Import from `payments.models` directly;
# don't re-export here to keep ownership unambiguous.


# ---------------------------------------------------------------------------
# OutboxEvent — transactional outbox for guaranteed event delivery
# ---------------------------------------------------------------------------

class OutboxEvent(models.Model):
    """
    Transactional outbox: events written atomically with domain changes.

    Two-track semantics (Block C HTTP publisher, founder verdict 2026-05-30,
    memory ``project_outbox_dual_delivery_fields``):

    * ``local_processed_at`` — local Ayla handlers (notifications, cache
      invalidation, log stubs) ran successfully. ``processed_at`` is the
      legacy alias kept in lock-step during the transition window.
    * ``bot_delivery_status`` (+ companion fields) — cross-service
      delivery to ``external_target`` (default ``bot-platform``). HTTP
      publisher (Block C) sets these.

    Codex P0-4 root cause: today's dispatcher conflates "local notification
    succeeded" with "external consumer received the event". Splitting the
    state markers removes that ambiguity. Until Block C publisher lands
    ``external_delivery_enabled`` stays ``False`` so existing rows are
    untouched and the dispatcher behaves identically.
    """

    class Topic(models.TextChoices):
        BOOKING_CREATED = "booking.created", "Запись создана"
        BOOKING_CONFIRMED = "booking.confirmed", "Запись подтверждена"
        BOOKING_CANCELLED = "booking.cancelled", "Запись отменена"
        BOOKING_RESCHEDULED = "booking.rescheduled", "Запись перенесена"
        BOOKING_COMPLETED = "booking.completed", "Запись завершена"
        BOOKING_NO_SHOW = "booking.no_show", "Клиент не пришёл"
        # B-1a (Block B, Variant C): renamed payment.confirmed →
        # payment.captured to align with the ADR cross-service vocabulary
        # bot-platform already consumes (apps/eventbus/consumers/payment.py
        # registers payment.authorized / payment.captured / payment.failed
        # / payment.refunded). Ayla's old payment.confirmed name was
        # rejected by bot's ingest as unknown → codex P0-1.
        # payment.authorized intentionally NOT emitted here — the hold
        # lifecycle stays on booking.confirmed during pilot (Variant C
        # decision, event-contract pilot vocabulary addendum 2026-06-01).
        PAYMENT_CAPTURED = "payment.captured", "Оплата захвачена"
        # B-1b — payment.failed (v1) emitted when YooKassa cancels or
        # fails a payment. Data carries id / enum / numbers only, no
        # PII per event-contract §7 (no names / emails / card numbers /
        # free-text reasons). Bot-side handler (W2 territory) owns the
        # retry threshold + customer DM.
        PAYMENT_FAILED = "payment.failed", "Оплата не прошла"
        PAYMENT_REFUNDED = "payment.refunded", "Возврат оплаты"
        CACHE_INVALIDATE_SLOTS = "cache.invalidate_slots", "Инвалидация кеша слотов"
        TENANT_RELATIONSHIP_REVOKED = (
            "tenant.relationship.revoked",
            "TenantUserRelationship отозван (#246 Q1)",
        )

    class BotDeliveryStatus(models.TextChoices):
        # Default — publisher has not attempted delivery yet.
        PENDING = "pending", "Ожидает доставки"
        # HTTP request fired and bot-platform returned 2xx (synchronous
        # ack). Some endpoints will return 202 + async ack later → state
        # advances to ACKNOWLEDGED when the matching ingest receipt is
        # observed.
        SENT = "sent", "Отправлено"
        ACKNOWLEDGED = "acknowledged", "Подтверждено получателем"
        # Transient failure (network / 5xx / timeout / 429). Publisher
        # bumps ``bot_attempt_count`` and sets ``bot_next_retry_at``.
        FAILED = "failed", "Ошибка доставки (ретрай возможен)"
        # Terminal state — exceeded retry budget or permanent 4xx.
        # Ops needs to inspect and replay via the future replay command.
        DEAD = "dead", "Письмо мертво (DLQ)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=50, choices=Topic.choices)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Legacy "row has been touched by any processor" marker. Kept
    # functional during transition: existing dispatcher continues to
    # set this so old SQL filters (``WHERE processed_at IS NULL``) still
    # work. New publisher writes to ``local_processed_at`` AND
    # ``processed_at`` together until the next migration removes this.
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    # --- Block C dual-delivery extension (founder 2026-05-30) -----------
    # Local handler completion timestamp. Distinct from cross-service
    # delivery: rows can be ``local_processed_at IS NOT NULL`` while
    # ``bot_delivery_status = pending`` (the bot publisher just has not
    # picked them up yet).
    local_processed_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text=(
            "When local Ayla handlers finished. Cross-service delivery "
            "tracked separately via bot_delivery_status."
        ),
    )
    # Per-row opt-in switch. The publisher only ships rows where this
    # is True. Defaulting to False keeps the migration backward compat
    # — historical rows and topics not yet promoted to cross-service
    # ignore the new fields entirely.
    external_delivery_enabled = models.BooleanField(
        default=False,
        help_text=(
            "If False, publisher skips this row. Toggled True per topic "
            "as Block C wires each cross-service event into the contract."
        ),
    )
    external_target = models.CharField(
        max_length=64,
        default="bot-platform",
        help_text=(
            "Logical destination identifier. ``bot-platform`` for the "
            "ADR-0009 cross-service ingest; future targets may be added."
        ),
    )
    bot_delivery_status = models.CharField(
        max_length=20,
        choices=BotDeliveryStatus.choices,
        default=BotDeliveryStatus.PENDING,
        db_index=True,
    )
    bot_delivered_at = models.DateTimeField(null=True, blank=True)
    bot_attempt_count = models.PositiveSmallIntegerField(default=0)
    bot_next_retry_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Earliest time the publisher may retry this row.",
    )
    bot_last_error = models.TextField(blank=True, default="")
    bot_response_status = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="HTTP status of the most recent bot-platform response.",
    )
    bot_dead_lettered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Outbox Event'
        verbose_name_plural = 'Outbox Events'
        indexes = [
            # Publisher scan: pull rows that need cross-service delivery
            # and are eligible right now (not blocked by retry backoff).
            # Composite covers the WHERE chain
            #   external_delivery_enabled = True
            #   AND bot_delivery_status IN ('pending', 'failed')
            #   AND (bot_next_retry_at IS NULL OR bot_next_retry_at <= now)
            # without scanning rows that opt out of external delivery.
            models.Index(
                fields=['external_delivery_enabled', 'bot_delivery_status', 'bot_next_retry_at'],
                name='outbox_publisher_scan_idx',
            ),
        ]

    def __str__(self) -> str:
        status = "processed" if self.processed_at else "pending"
        return f"{self.topic} ({status}) — {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def data(self) -> dict:
        """Domain payload inside the ADR-0009 envelope.

        Convenience accessor for handlers. After issue #486 every new
        OutboxEvent.payload is the full envelope (event_id, event_name,
        event_version, …, data: {...}). Handlers want the inner `data`
        dict 99% of the time. Rows written before #486 stored the
        domain payload directly under .payload with no wrapper — for
        those the .get('data', self.payload) fallback returns the
        legacy shape unchanged, so the property is a no-op on history.
        """
        # isinstance check guards a misshaped payload (e.g. someone
        # writes None via a buggy migration). Treat both `payload['data']
        # is None` and `payload` not a dict as legacy / no-envelope.
        if not isinstance(self.payload, dict):
            return {}
        inner = self.payload.get("data")
        return inner if isinstance(inner, dict) else self.payload


# ---------------------------------------------------------------------------
# IdempotencyKey — replay protection for mutating endpoints (#512)
# ---------------------------------------------------------------------------

class IdempotencyKey(models.Model):
    """Replay protection for mutating booking endpoints.

    POST /appointments/ already uses ``Appointment.idempotency_key`` for
    create-time deduplication. ``cancel`` and ``reschedule`` (and any
    future mutating endpoint) need their own keys — the same appointment
    can be rescheduled twice, each call needs a distinct key.

    Lookup key: ``(key, operation_name, user)``. Different users may
    submit the same X-Idempotency-Key value; same user submitting the
    same key for the same operation = replay.

    Body-hash check: the request body is SHA256-hashed at first call;
    a replay with a DIFFERENT body for the same key raises 422 conflict
    (the client is misusing the key). Same hash → return cached response.

    TTL: ``expires_at`` defaults to 24h after creation. The
    ``appointments.purge_expired_idempotency_keys`` Celery beat task
    (see settings.base CELERY_BEAT_SCHEDULE) drops expired rows.
    """

    DEFAULT_TTL = timedelta(hours=24)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='idempotency_keys',
    )
    key = models.CharField(
        max_length=128,
        help_text="Client-provided X-Idempotency-Key header value.",
    )
    operation_name = models.CharField(
        max_length=64,
        help_text=(
            "Operation identifier, e.g. 'booking.cancel'. Scoped so a "
            "client can reuse the same key value across different "
            "operations without collision."
        ),
    )
    target_type = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Optional model name of the target row (e.g. 'Appointment').",
    )
    target_id = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Optional target row id (UUID stringified) for audit only.",
    )
    request_body_hash = models.CharField(
        max_length=64,
        help_text="SHA256 of the normalised request body. Mismatched on replay = 422.",
    )
    response_status = models.PositiveSmallIntegerField()
    response_payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = 'Idempotency Key'
        verbose_name_plural = 'Idempotency Keys'
        constraints = [
            # target_id IS in the constraint to prevent cross-target
            # cache bleed: cancel(apt_X) and cancel(apt_Y) with the same
            # X-Idempotency-Key (a common mobile retry-buffer reuse
            # pattern) must NOT return apt_X's cached response for
            # apt_Y's request. Without target_id the helper would
            # silently return cached 200 → apt_Y never cancelled →
            # customer double-booked. See PR #143 adversarial review.
            models.UniqueConstraint(
                fields=['user', 'operation_name', 'key', 'target_id'],
                name='idempotency_unique_user_op_key_target',
            ),
        ]
        indexes = [
            models.Index(
                fields=['user', 'operation_name', 'key', 'target_id'],
                name='idempotency_lookup_idx',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.operation_name}:{self.key[:12]}… (user={self.user_id})"
