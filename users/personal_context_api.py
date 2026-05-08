"""UserPersonalContext API — DRF-174 minimum-lovable subset.

Endpoints (per Notion API Spec v2.0 §AI ПЕРСОНАЛИЗАЦИЯ):

  GET    /api/v1/users/me/personal-context/   — read all fields
  PATCH  /api/v1/users/me/personal-context/   — update any subset

Deferred to Phase 6 per CEO scope reduction:

  DELETE /api/v1/users/me/personal-context/{field}/   — "забудь X"
  POST   /api/v1/users/me/personal-context/skip       — anti-spam cooldown

These need PersonalizationEngine (extraction + skipped_questions tracking)
which is also deferred. The minimum subset shipped here lets mobile collect
explicit preferences through onboarding UI without us building the inference
layer speculatively.

## Auto-creation

A user's `UserPersonalContext` row is created on first PATCH, not via
signal. This avoids one row per guest who never personalises and keeps
the user table's signal handlers simple.
"""
from __future__ import annotations

from decimal import Decimal

from drf_spectacular.utils import extend_schema
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import UserPersonalContext
from users.permissions import IsClient, IsClientApp
from users.response import success_response


# Validation: TimeSlot values mobile can pick from.
_VALID_TIME_SLOTS = {
    choice.value for choice in UserPersonalContext.TimeSlot
}


class PersonalContextSerializer(serializers.ModelSerializer):
    """Read + write shape for `/users/me/personal-context/`.

    Validation pulled into ``validate_*`` methods rather than relying on
    DRF's default JSONField behaviour because we want to enforce the
    enum-of-strings contract for ``preferred_time_slots`` without
    introducing a separate lookup table at this stage.
    """

    class Meta:
        model = UserPersonalContext
        fields = [
            "preferred_districts",
            "preferred_time_slots",
            "price_range_min",
            "price_range_max",
            "diet_type",
            "skin_sensitivities",
            "prefers_flexible_cancellation",
            "updated_at",
        ]
        read_only_fields = ("updated_at",)

    def validate_preferred_districts(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of strings")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise serializers.ValidationError("Each item must be a non-empty string")
            if len(item) > 100:
                raise serializers.ValidationError("Item too long (max 100 chars)")
        return [s.strip() for s in value][:20]  # cap at 20

    def validate_preferred_time_slots(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of TimeSlot keys")
        invalid = [v for v in value if v not in _VALID_TIME_SLOTS]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown time slots: {invalid}. "
                f"Allowed: {sorted(_VALID_TIME_SLOTS)}"
            )
        return list(dict.fromkeys(value))  # dedupe preserve order

    def validate_skin_sensitivities(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of strings")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise serializers.ValidationError("Each item must be a non-empty string")
            if len(item) > 100:
                raise serializers.ValidationError("Item too long (max 100 chars)")
        return [s.strip() for s in value][:30]

    def validate(self, attrs):
        lo = attrs.get("price_range_min")
        hi = attrs.get("price_range_max")
        # PATCH only sends changed fields, so lo/hi may not both be present.
        # Cross-field check: if both present, hi >= lo.
        if lo is not None and hi is not None and hi < lo:
            raise serializers.ValidationError({
                "price_range_max": "must be >= price_range_min",
            })
        # Either bound can't be negative.
        for field in ("price_range_min", "price_range_max"):
            v = attrs.get(field)
            if v is not None and v < Decimal("0"):
                raise serializers.ValidationError({field: "must be non-negative"})
        return attrs


class PersonalContextView(APIView):
    """GET / PATCH `/api/v1/users/me/personal-context/`.

    🟢 client-only. No anonymous access — requires real registered user
    because the data attaches to user_id and only makes sense for
    accounts that survive across sessions.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    serializer_class = PersonalContextSerializer

    @extend_schema(responses={200: PersonalContextSerializer})
    def get(self, request: Request) -> Response:
        ctx = self._get_or_default(request.user)
        return success_response(PersonalContextSerializer(ctx).data)

    @extend_schema(
        request=PersonalContextSerializer,
        responses={200: PersonalContextSerializer},
    )
    def patch(self, request: Request) -> Response:
        ctx, _ = UserPersonalContext.objects.get_or_create(user=request.user)
        serializer = PersonalContextSerializer(
            ctx, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return success_response(serializer.data)

    @staticmethod
    def _get_or_default(user) -> UserPersonalContext:
        """Return the row if it exists, else an unsaved instance with
        defaults — saves the GET caller from auto-creating empty rows."""
        try:
            return user.personal_context
        except UserPersonalContext.DoesNotExist:
            return UserPersonalContext(user=user)
