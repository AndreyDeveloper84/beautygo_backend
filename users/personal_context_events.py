"""DRF-230 PR 3 — analytics emit helpers for the personalization layer.

Thin wrapper around ``analytics.AnalyticsEvent.objects.create`` so
callers don't have to know the column layout. Four events:

- ``profile_question_shown`` — surface asked a profile question
- ``profile_question_answered`` — user typed a value
- ``profile_question_skipped`` — user dismissed without answering
- ``context_used_in_recommendation`` — a recommendation pulled at
  least one field from UserPersonalContext as input

Failure to write an event must NEVER break the call site (we'd rather
lose a metric than fail a feature). All emit_* helpers swallow
exceptions and log at WARNING level.
"""
from __future__ import annotations

import logging

from analytics import event_catalogue


logger = logging.getLogger("users.personalization.events")


def _emit(user, event_name: str, payload: dict) -> None:
    """Internal helper. Best-effort write; never raises.

    Server-side emit fills the required AnalyticsEvent fields the API
    ingestion layer would otherwise demand from the client: a fresh
    UUID for ``client_event_id`` (no idempotency duplicate on
    server-generated events) and ``app_type=client`` (Pro app has no
    path to write personal-context, so the constant is safe).
    """
    import uuid

    try:
        from analytics.models import AnalyticsEvent
        AnalyticsEvent.objects.create(
            actor=user,
            event_name=event_name,
            payload=payload or {},
            app_type=AnalyticsEvent.AppType.CLIENT,
            client_event_id=uuid.uuid4(),
        )
    except Exception as exc:  # pragma: no cover - belt + suspenders
        logger.warning(
            "personalization.event_emit_failed event=%s user=%s err=%s",
            event_name, getattr(user, "pk", None), exc,
        )


def emit_question_shown(user, *, field: str, surface: str) -> None:
    _emit(user, event_catalogue.PROFILE_QUESTION_SHOWN, {
        "field": field, "surface": surface,
    })


def emit_question_answered(user, *, field: str, surface: str) -> None:
    _emit(user, event_catalogue.PROFILE_QUESTION_ANSWERED, {
        "field": field, "surface": surface,
    })


def emit_question_skipped(user, *, field: str, surface: str,
                          skip_count: int) -> None:
    _emit(user, event_catalogue.PROFILE_QUESTION_SKIPPED, {
        "field": field, "surface": surface, "skip_count": skip_count,
    })


def emit_context_used(user, *, fields_used: list[str], surface: str,
                      recommendation_id: str | None = None) -> None:
    _emit(user, event_catalogue.CONTEXT_USED_IN_RECOMMENDATION, {
        "fields_used": fields_used,
        "surface": surface,
        "recommendation_id": recommendation_id,
    })
