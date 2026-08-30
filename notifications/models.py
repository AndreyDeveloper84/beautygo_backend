"""Notification persistence — one row per dispatched message.

Stored for two reasons:

1. Audit / debugging — when ops asks "did the 1h reminder go out?", the
   row + ``status`` answers without grepping Celery logs.
2. Idempotency for the reminder beat — before scheduling the 1h reminder
   for an appointment we check whether a row already exists for
   ``(user, template_id='appointment_reminder_1h', appointment_id)``.

The model is deliberately wide rather than normalized — we keep the
rendered title/body and the original context (``data``) so we can replay
or display in an in-app feed without re-rendering against possibly-changed
templates.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Channel(models.TextChoices):
        PUSH = "push", "Push"
        SMS = "sms", "SMS"
        BOTH = "both", "Push + SMS fallback"

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Ошибка"
        SKIPPED = "skipped", "Пропущено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    template_id = models.CharField(max_length=64)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    deep_link = models.CharField(max_length=512, blank=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error = models.TextField(blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # "-id" makes the order total — see DRF-1128. created_at is
        # auto_now_add, so a dispatch batch ties on it.
        ordering = ["-created_at", "-id"]
        indexes = [
            # Reminder de-dup query: filter by user + template + appointment_id (in data).
            # The (user, template_id, created_at) index covers the
            # "is there already a reminder for this user × template?" lookup.
            models.Index(fields=["user", "template_id", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.template_id} → {self.user_id} ({self.status})"
