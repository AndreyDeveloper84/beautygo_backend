"""Nutrition persistence — FoodScan one row per /scan/ request.

Stored regardless of provider success so we have:
- Audit trail (which provider, when, latency, raw response) for support
  disputes ("why did Ayla say my borscht had 800 kcal?")
- Cost attribution per user / per provider for the daily $-spend dashboard
- Replay for the "scan again" feature without re-uploading the photo
- Slice 3 nutrition lookup runs against this row, not against the wire
  response, so failed lookups can be retried offline

The image itself is in S3 (django-storages) — we keep just the storage
key on this row. Photo TTL on S3 is 30 days per docs/FOOD_SCANNER_DECISION.md
and is enforced at the bucket lifecycle level, not Django-side.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


def _scan_image_path(instance: "FoodScan", filename: str) -> str:
    return f"food-scans/{instance.user_id}/{instance.id}.jpg"


class FoodScan(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI Vision"
        YANDEX = "yandex", "Yandex Vision"
        VIT_SELF_HOST = "vit-self-host", "Self-host ViT"  # Phase 6

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="food_scans",
    )
    image = models.ImageField(upload_to=_scan_image_path)

    # Recognition result (from provider).
    dish_name = models.CharField(max_length=200, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    portion_g = models.FloatField(null=True, blank=True)
    ingredients = models.JSONField(default=list, blank=True)

    # Nutrition (filled by Slice 3 lookup; nullable for now).
    nutrition = models.JSONField(null=True, blank=True)

    # Provider audit.
    provider_used = models.CharField(
        max_length=24, choices=Provider.choices, blank=True, default="",
    )
    provider_fallback_from = models.CharField(
        max_length=24,
        blank=True,
        default="",
        help_text="If the primary provider failed, which one was tried first.",
    )
    latency_ms = models.IntegerField(default=0)
    raw_response = models.JSONField(default=dict, blank=True)

    # Failure tracking (when no provider returned a confident result).
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Food Scan"
        verbose_name_plural = "Food Scans"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["provider_used"]),
        ]

    def __str__(self) -> str:
        return f"{self.dish_name or '?'} ({self.provider_used}, conf={self.confidence:.2f})"
