"""Analytics event ingestion model.

The mobile app emits structured events ("food_scan_taken",
"booking_created", "context_question_skipped", etc.) — this model
is the durable raw store. Aggregation / dashboards run as SELECT
queries in BI tools (Metabase, Superset) against this single table;
no ORM-side aggregation lives here.

## Schema decisions

- **`event_name: CharField(64)`** — flat string, indexed, no FK to a
  catalogue table. The catalogue lives in a Notion doc + a runtime
  whitelist in `analytics/event_catalogue.py` (validated by the
  serializer). New events ship without migrations: bump the
  whitelist + push.
- **`payload: JSONField`** — per-event fields (durations, IDs,
  amounts). Schema-on-read. We trust mobile to keep types stable
  per event_name; if it doesn't, BI queries skip the bad rows.
- **Provenance fields** — `actor`, `app_type`, `tenant`, `created_at`,
  `device_token_id`, `client_event_id` answer "where did this come
  from?" for any future audit / re-aggregation.
- **`client_event_id` (idempotency)** — mobile sends a UUID with
  every event; we dedupe per `(actor, client_event_id)`. Clients
  retry-on-network-error without spawning duplicate rows.
- **`actor` is nullable** — anonymous flows can emit events too
  (welcome screen taps, anonymous chat messages). Authenticated
  events fill `actor`; anonymous fill `anonymous_session_id` instead.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class AnalyticsEvent(models.Model):
    class AppType(models.TextChoices):
        CLIENT = "client", "Ayla (client)"
        PRO = "pro", "Ayla Pro (specialist)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Event identity ──
    event_name = models.CharField(
        max_length=64, db_index=True,
        help_text="Validated against analytics.event_catalogue.EVENT_NAMES.",
    )
    payload = models.JSONField(default=dict, blank=True)

    # ── Provenance ──
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="analytics_events",
        help_text="NULL for anonymous emissions (anonymous_session_id "
                  "carries the session instead).",
    )
    anonymous_session_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="Set when actor is NULL (welcome / pre-OTP flow).",
    )
    app_type = models.CharField(
        max_length=8, choices=AppType.choices, db_index=True,
        help_text="Mirrors X-App-Type header — events from Pro app vs "
                  "client app are first-class separate streams.",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="analytics_events",
        help_text="Denormalised from request.tenant; allows a future "
                  "dashboard to scope events per tenant. Nullable for "
                  "single-tenant rollout phase.",
    )

    # ── Idempotency ──
    client_event_id = models.UUIDField(
        help_text="UUID generated client-side; (actor, client_event_id) "
                  "is unique so retries are safe.",
    )

    # ── When it happened (client time AND server time) ──
    client_timestamp = models.DateTimeField(
        null=True, blank=True,
        help_text="Mobile-side timestamp at emission. Trust-but-verify; "
                  "use server `created_at` for cohort analysis.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Analytics Event"
        verbose_name_plural = "Analytics Events"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_name", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["tenant", "event_name", "-created_at"]),
        ]
        constraints = [
            # Per-actor idempotency. NULL actors (anonymous events) fall
            # back to the anonymous_session_id partial unique below; we
            # don't conflate the two namespaces.
            models.UniqueConstraint(
                fields=["actor", "client_event_id"],
                condition=models.Q(actor__isnull=False),
                name="analytics_event_unique_per_actor",
            ),
            models.UniqueConstraint(
                fields=["anonymous_session_id", "client_event_id"],
                condition=models.Q(anonymous_session_id__isnull=False),
                name="analytics_event_unique_per_anon_session",
            ),
        ]

    def __str__(self) -> str:
        owner = self.actor_id or self.anonymous_session_id or "?"
        return f"{self.event_name} @ {self.created_at:%Y-%m-%d %H:%M} ({owner})"
