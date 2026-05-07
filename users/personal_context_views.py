"""DRF-230 — UserPersonalContext API.

5 endpoints under ``/api/v1/users/me/personal-context/``:

- ``GET`` — read full context (lazy-create on first access)
- ``PATCH`` — partial update (single field or batch)
- ``POST /skip/`` — record a skipped question (anti-spam tracking)
- ``DELETE /<field>/`` — clear a single field (152-ФЗ "забудь X")
- ``DELETE /`` — total wipe of context (152-ФЗ "очистить историю")

Anti-spam engine + Celery infer task ship in separate PRs (PR 2 + PR 3
of DRF-230). This PR is the foundation: model + endpoints + Django
admin so mobile/bot can start writing into the context immediately.

Design notes:

- Lazy creation of the row on first read keeps DB clean for guests
  who never personalise. ``OneToOneField(user)`` keeps lookup O(1).
- ``PATCH`` only writes fields explicitly present in the body; missing
  keys are NOT cleared (use DELETE /<field>/ for that).
- DELETE /<field>/ resets to the model default (``""`` for char,
  ``[]`` for list, ``None`` for nullable). 152-ФЗ-compliant audit
  log lives at INFO level.
- Total wipe deletes the row entirely (next GET creates a fresh one).
- Internal endpoints (bot/service-to-service) are NOT exposed here —
  bot resolves user via JWT just like mobile (the maxbot
  ``ProxyUser`` flows through the same path).
"""
from __future__ import annotations

import logging

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import UserPersonalContext
from users.response import error_response, success_response

logger = logging.getLogger("users.personal_context")


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


# Fields the API exposes for read + PATCH. Keep in sync with the model;
# DRF-230 PR 1 ships the green-zone (公개) fields only — yellow / red
# zones land in Phase 2 with extra encryption.
_GREEN_ZONE_FIELDS = (
    # DRF-174 baseline
    "preferred_districts",
    "preferred_time_slots",
    "price_range_min",
    "price_range_max",
    "diet_type",
    "skin_sensitivities",
    "prefers_flexible_cancellation",
    # DRF-230 M3 P0 additions
    "workplace_district",
    "home_district",
    "favorite_masters",
    "min_rating_preference",
    "busy_days",
)

# Service-bookkeeping fields are read-only on the API surface —
# PersonalizationEngine writes them, the user can't.
_SERVICE_FIELDS = ("skipped_questions", "last_asked_at", "data_sources")


class UserPersonalContextSerializer(serializers.ModelSerializer):
    """Read-write serializer for the green zone, read-only for service fields."""

    class Meta:
        model = UserPersonalContext
        fields = ("id", "user", *_GREEN_ZONE_FIELDS, *_SERVICE_FIELDS,
                  "created_at", "updated_at")
        read_only_fields = (
            "id", "user", *_SERVICE_FIELDS, "created_at", "updated_at",
        )

    def validate_min_rating_preference(self, value):
        if value is None:
            return value
        if not 0.0 <= float(value) <= 5.0:
            raise serializers.ValidationError(
                "min_rating_preference must be between 0.0 and 5.0",
            )
        return value

    def validate_preferred_time_slots(self, value):
        valid = {choice.value for choice in UserPersonalContext.TimeSlot}
        for slot in value or []:
            if slot not in valid:
                raise serializers.ValidationError(
                    f"Invalid time slot: {slot}",
                )
        return value


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _get_or_create_context(user) -> UserPersonalContext:
    """Lazy-create on first access. Idempotent."""
    obj, _ = UserPersonalContext.objects.get_or_create(user=user)
    return obj


class UserPersonalContextView(APIView):
    """GET / PATCH / DELETE the whole context."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        ctx = _get_or_create_context(request.user)
        return success_response(
            UserPersonalContextSerializer(ctx).data,
            status_code=status.HTTP_200_OK,
        )

    def patch(self, request: Request) -> Response:
        ctx = _get_or_create_context(request.user)
        serializer = UserPersonalContextSerializer(
            ctx, data=request.data, partial=True,
        )
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid context update",
                details=serializer.errors,
            )
        serializer.save()
        return success_response(
            serializer.data,
            status_code=status.HTTP_200_OK,
        )

    def delete(self, request: Request) -> Response:
        # 152-ФЗ total wipe — delete the row entirely. Next GET will
        # lazy-create a fresh empty one.
        UserPersonalContext.objects.filter(user=request.user).delete()
        logger.info(
            "personal_context.wiped user=%s reason=152-fz",
            request.user.pk,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserPersonalContextFieldDeleteView(APIView):
    """DELETE /api/v1/users/me/personal-context/<field>/.

    152-ФЗ-compatible per-field reset. Resets the field to its model
    default (empty string for char, [] for list, None for nullable
    decimal/float, False for boolean). Service fields cannot be wiped
    via this endpoint — they're managed by PersonalizationEngine.
    """

    permission_classes = [IsAuthenticated]

    _FIELD_DEFAULTS: dict[str, object] = {
        "preferred_districts": list,
        "preferred_time_slots": list,
        "price_range_min": lambda: None,
        "price_range_max": lambda: None,
        "diet_type": str,
        "skin_sensitivities": list,
        "prefers_flexible_cancellation": lambda: False,
        "workplace_district": str,
        "home_district": str,
        "favorite_masters": list,
        "min_rating_preference": lambda: None,
        "busy_days": list,
    }

    def delete(self, request: Request, field: str) -> Response:
        if field not in self._FIELD_DEFAULTS:
            return error_response(
                "FIELD_NOT_RESETTABLE",
                f"Field '{field}' is not user-resettable. Service fields "
                "are managed by the personalization engine.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        ctx = _get_or_create_context(request.user)
        default_factory = self._FIELD_DEFAULTS[field]
        setattr(ctx, field, default_factory())
        ctx.save(update_fields=[field, "updated_at"])
        logger.info(
            "personal_context.field_reset user=%s field=%s reason=152-fz",
            request.user.pk, field,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserPersonalContextSkipView(APIView):
    """POST /api/v1/users/me/personal-context/skip/.

    Records that the user dismissed a personalization question. Body:
    ``{"field": "workplace_district"}``. Increments the per-field
    skipped_questions counter and stamps last_at. The full anti-spam
    rule engine (skip × 2 → 30-day pause) lives in the
    PersonalizationEngine PR; this endpoint just persists the signal.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        from datetime import datetime, timezone
        field = (request.data or {}).get("field")
        if not field or field not in UserPersonalContextFieldDeleteView._FIELD_DEFAULTS:
            return error_response(
                "VALIDATION_ERROR",
                "Body must include 'field' matching a user-resettable name.",
            )
        ctx = _get_or_create_context(request.user)
        skipped = dict(ctx.skipped_questions or {})
        entry = dict(skipped.get(field) or {})
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_at"] = datetime.now(timezone.utc).isoformat()
        skipped[field] = entry
        ctx.skipped_questions = skipped
        ctx.save(update_fields=["skipped_questions", "updated_at"])
        return success_response(
            {"field": field, "count": entry["count"]},
            status_code=status.HTTP_200_OK,
        )
