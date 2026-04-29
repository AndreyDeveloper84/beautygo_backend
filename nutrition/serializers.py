"""DRF serializers for nutrition endpoints.

Slice 2 ships POST /scan/ — the request takes multipart/form-data with
the image; the response shape mirrors the spec's ``FoodScanResponse``.
"""
from __future__ import annotations

from rest_framework import serializers

from nutrition.models import FoodScan


# 10 MiB — same cap used by the portfolio uploader; the mobile client
# should compress before sending but the server is the only enforcer
# the user can't disable.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ScanRequestSerializer(serializers.Serializer):
    """multipart/form-data: image (required) + portion_multiplier (optional)."""

    image = serializers.ImageField()
    portion_multiplier = serializers.FloatField(
        required=False, default=1.0, min_value=0.1, max_value=10.0,
    )

    def validate_image(self, value):
        if value.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError(
                f"Image must be ≤ {MAX_IMAGE_BYTES // (1024 * 1024)} MiB"
            )
        ctype = getattr(value, "content_type", "") or ""
        if ctype and ctype not in ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                f"Unsupported content type {ctype}. "
                f"Allowed: {sorted(ALLOWED_CONTENT_TYPES)}"
            )
        return value


class FoodScanResponseSerializer(serializers.ModelSerializer):
    """Response shape per Notion API Spec v2.0 §FOOD SCANNER FoodScanResponse.

    The DB ``FoodScan.nutrition`` field stores a richer dataclass JSON
    (per-100g values, matched_dish, source, ...) for analytics, replay,
    and Slice 3b's FoodLog derivation. The wire response is the lean
    spec shape: ``{calories, protein_g, fat_g, carbs_g, vitamins}``.
    """

    scan_id = serializers.UUIDField(source="id", read_only=True)
    provider = serializers.CharField(source="provider_used", read_only=True)
    nutrition = serializers.SerializerMethodField()
    # Per spec v2.0 §FOOD SCANNER — beauty_insights field present in
    # response shape, nullable. Slice 3+ will populate when nutrition
    # lookup runs and we have data to derive vitamin deficits etc.
    beauty_insights = serializers.SerializerMethodField()

    class Meta:
        model = FoodScan
        fields = [
            "scan_id",
            "dish_name",
            "confidence",
            "portion_g",
            "ingredients",
            "nutrition",
            "beauty_insights",
            "provider",
            "latency_ms",
            "created_at",
        ]
        read_only_fields = fields

    def get_nutrition(self, obj: FoodScan):
        """Transform rich internal JSON → spec FoodScanResponse.nutrition shape.

        Returns null when the seed lookup missed (mobile prompts manual
        entry). Vitamins is empty map for now — seed has no vitamin data;
        Slice 3a' OFF/USDA lookup will populate.
        """
        n = obj.nutrition
        if not n:
            return None
        return {
            "calories": n.get("kcal"),
            "protein_g": n.get("protein_g"),
            "fat_g": n.get("fat_g"),
            "carbs_g": n.get("carbs_g"),
            "vitamins": {},
        }

    def get_beauty_insights(self, obj: FoodScan):  # noqa: ARG002 — Slice 3+ fills this
        return None
