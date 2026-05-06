"""Analytics app — generic event ingestion endpoint.

Single responsibility: persist mobile-emitted events with provenance
(actor, app_type, idempotency, timestamp, payload). Cohort-level
analysis lives in dashboards / SQL — this app is intake-only.
"""
from __future__ import annotations

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "analytics"
