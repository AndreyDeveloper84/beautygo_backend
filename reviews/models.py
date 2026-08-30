"""Review model — client feedback on completed appointments."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Review(models.Model):
    """A review left by a client after a completed appointment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # OneToOne: one review per appointment
    appointment = models.OneToOneField(
        'appointments.Appointment',
        on_delete=models.CASCADE,
        related_name='review',
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews_written',
    )
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='reviews',
    )
    service = models.ForeignKey(
        'services.Service',
        on_delete=models.CASCADE,
        related_name='reviews',
    )

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    text = models.TextField(blank=True, max_length=1000)

    is_anonymous = models.BooleanField(
        default=False,
        help_text="Hide client name in public listing",
    )
    specialist_reply = models.TextField(
        blank=True, null=True,
        help_text="Specialist response to this review",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="Hidden by moderation — excluded from rating",
    )

    # tenant FK — DRF-242.6. Denormalised from appointment.specialist.tenant
    # so per-tenant aggregates (avg rating, review counts) avoid a 3-hop JOIN.
    # PROTECT against silent data loss on tenant delete; invariant maintained
    # by backfill + service layer.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviews",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # '-id' makes the order total — see DRF-1128. created_at is
        # auto_now_add, so a batch written in one transaction ties on it.
        ordering = ['-created_at', '-id']
        verbose_name = 'Отзыв'
        verbose_name_plural = 'Отзывы'
        indexes = [
            models.Index(fields=['specialist', 'is_hidden', '-created_at'],
                         name='review_specialist_listing_idx'),
            models.Index(fields=['client'], name='review_client_idx'),
            # Per-tenant review aggregates — DRF-242.7. Marketplace
            # admin pulls recent reviews by tenant for moderation.
            models.Index(
                fields=['tenant', '-created_at'],
                name='review_tenant_created_idx',
            ),
        ]

    def __str__(self) -> str:
        return f"Review by {self.client_id} — {self.rating}★ for {self.specialist_id}"
