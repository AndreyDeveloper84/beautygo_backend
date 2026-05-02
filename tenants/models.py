"""Tenant model — multi-tenant foundation (DRF-242.1).

Phase A.7 of the AI Chat foundation. This module is intentionally narrow:
just the Tenant table + manager. Wiring to existing models (FK from
Conversation, User, SpecialistProfile, Appointment, etc.) happens in
follow-up tickets so each conversion is a small, reviewable diff.

Scoping middleware and feature-flag rollout (DRF-242.4/.5) are also
deferred — for now Tenant is purely a registry table.

Why a dedicated app instead of inlining into ``users``:
- Tenant is a cross-cutting domain concept, not a user-system concept.
- Future ticket may add ``TenantSubscription`` / ``TenantBilling`` /
  ``TenantFeatureFlag`` rows without polluting ``users.models``.
- Independent migration history simplifies rollback if the
  multi-tenant rollout has to pause mid-flight.

Why no BaseModel inheritance:
- The codebase uses inline ``UUIDField(primary_key=True, ...)`` per model
  (see ``users.User``, ``ai.Conversation``, ``nutrition.FoodScan``). New
  introduction of a BaseModel would be its own refactor — out of scope.
"""
from __future__ import annotations

import re
import uuid

from django.db import models


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


class _ActiveTenantManager(models.Manager):
    """Default manager: hides ``is_active=False`` tenants from app code.

    Use ``Tenant.all_objects`` in admin / billing to see deactivated rows.
    Pattern matches ``ai.Conversation._ConversationManager`` in this repo.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class Tenant(models.Model):
    """A logical isolation boundary for marketplace data (DRF-242).

    The MVP shape is deliberately minimal — just enough to scope foreign
    keys later. Pricing, feature flags, branding, etc. are future fields
    or sibling tables.

    Slug is the wire identifier (URL paths, ``X-Tenant`` header values).
    Name is human-readable for admin/UI. ID is the canonical FK target.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(
        max_length=50,
        unique=True,
        help_text=(
            "Lowercase identifier used in URLs and the X-Tenant header. "
            "Letters, digits, hyphen, underscore. Must start with a letter "
            "or digit. Cannot be changed after creation."
        ),
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable name shown in admin and billing.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "False hides the tenant from default queries. Use this to "
            "soft-disable a tenant without dropping its data — billing "
            "freezes, scoping middleware returns 403."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = _ActiveTenantManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Тенант"
        verbose_name_plural = "Тенанты"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "slug"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    def clean(self) -> None:
        """Validate slug shape at the model layer.

        Django's SlugField allows uppercase letters and lone hyphens
        (e.g. `-foo-`); we want a stricter shape so slugs round-trip
        cleanly through URL paths and HTTP headers.
        """
        from django.core.exceptions import ValidationError

        super().clean()
        if self.slug and not _SLUG_RE.match(self.slug):
            raise ValidationError({
                "slug": (
                    "Slug must be lowercase alphanumeric (with - or _), "
                    "2–50 chars, and start with a letter or digit."
                ),
            })
