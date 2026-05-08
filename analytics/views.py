"""Analytics ingestion endpoint — POST /api/v1/analytics/event/.

Single endpoint, single responsibility: persist one event row.

## Auth model

Authenticated **OR** guest — every event from the moment the app
launches needs to land. Anonymous emissions are linked to the
user's `AnonymousSession` (the guest User row's OneToOne) so post-OTP
session merge can re-key them to the real account if we ever want.

## Idempotency

Mobile sends `client_event_id` (UUID) per event and retries on
network errors. The model's per-actor / per-anon-session unique
constraints make duplicate POSTs idempotent — second call returns
**200** with the existing row instead of erroring out, mirroring the
favourites-add pattern (DRF-72).

## Throttle

Scoped throttle `analytics_event` — high cap (mobile may batch
emit on session start). Set in settings/base.py.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers as drf_serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from analytics.models import AnalyticsEvent
from analytics.serializers import EventCreateSerializer
from core.errors import ErrorCode
from users.response import error_response, success_response


logger = logging.getLogger(__name__)


class AnalyticsEventView(APIView):
    """POST /api/v1/analytics/event/ — durable event ingestion.

    `IsAuthenticated` accepts both real and guest users (guest JWTs
    pass `is_authenticated=True`; we branch on `request.user.is_guest`
    for the provenance fields). No `IsClientApp`/`IsProApp`
    restriction — both apps emit telemetry, and we record `app_type`
    so cohorts can split client vs pro afterwards.
    """

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "analytics_event"

    @extend_schema(
        request=EventCreateSerializer,
        responses={
            200: inline_serializer(
                name="AnalyticsEventIdempotentResponse",
                fields={
                    "id": drf_serializers.UUIDField(),
                    "created": drf_serializers.BooleanField(),
                },
            ),
            201: inline_serializer(
                name="AnalyticsEventCreatedResponse",
                fields={
                    "id": drf_serializers.UUIDField(),
                    "created": drf_serializers.BooleanField(),
                },
            ),
            400: OpenApiResponse(description="Validation error or unknown event_name"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = EventCreateSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors or {}
            # Surface UNKNOWN_EVENT_NAME specifically so mobile can
            # branch on the exact problem (mismatched catalogue vs
            # generic input bug).
            event_name_errors = errors.get("event_name", [])
            if any(
                "Unknown event_name" in str(e) for e in event_name_errors
            ):
                return error_response(
                    ErrorCode.UNKNOWN_EVENT_NAME,
                    "Unknown event_name; update mobile catalogue.",
                    details=errors,
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                "Невалидные данные",
                details=errors,
            )

        validated = serializer.validated_data
        actor = request.user
        is_guest = bool(getattr(actor, "is_guest", False))

        defaults = {
            "event_name": validated["event_name"],
            "payload": validated.get("payload") or {},
            "app_type": getattr(request, "app_type", None) or "client",
            "tenant": getattr(request, "tenant", None),
            "client_timestamp": validated.get("client_timestamp"),
        }

        try:
            if is_guest:
                anon_session = self._anonymous_session_id(actor)
                event, created = AnalyticsEvent.objects.get_or_create(
                    anonymous_session_id=anon_session,
                    client_event_id=validated["client_event_id"],
                    defaults={**defaults, "actor": None},
                )
            else:
                event, created = AnalyticsEvent.objects.get_or_create(
                    actor=actor,
                    client_event_id=validated["client_event_id"],
                    defaults=defaults,
                )
        except IntegrityError:
            # Race between two concurrent retries — one already
            # inserted. Re-fetch + behave as idempotent 200.
            event = self._refetch(actor, validated["client_event_id"])
            created = False
            if event is None:
                logger.exception("analytics.race_lookup_failed")
                return error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Не удалось сохранить событие",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        return success_response(
            {"id": str(event.id), "created": created},
            status_code=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
        )

    @staticmethod
    def _anonymous_session_id(actor):
        """Resolve the AnonymousSession.id for a guest User.

        Guest Users are paired 1:1 with AnonymousSession; if the row
        is missing for any reason, we fall back to None (event still
        lands; just less linkable). Robustness over strictness here —
        analytics ingestion must never block the chat / booking flow.
        """
        try:
            return actor.anonymous_session.id
        except Exception:  # noqa: BLE001 — defensive: missing OneToOne
            return None

    @staticmethod
    def _refetch(actor, client_event_id):
        """Race-recovery: get the row that the IntegrityError implies exists."""
        if getattr(actor, "is_guest", False):
            return AnalyticsEvent.objects.filter(
                anonymous_session_id=AnalyticsEventView._anonymous_session_id(actor),
                client_event_id=client_event_id,
            ).first()
        return AnalyticsEvent.objects.filter(
            actor=actor, client_event_id=client_event_id,
        ).first()
