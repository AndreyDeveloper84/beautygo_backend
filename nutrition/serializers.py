"""DRF serializers for nutrition endpoints.

Slice 2 ships POST /scan/ — the request takes multipart/form-data with
the image; the response shape mirrors the spec's ``FoodScanResponse``.
"""
from __future__ import annotations

from rest_framework import serializers

from nutrition.models import FoodLog, FoodScan, WaterLog


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


# ---------------------------------------------------------------------------
# Food log (Slice 3b)
# ---------------------------------------------------------------------------


class FoodLogCreateSerializer(serializers.Serializer):
    """Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION FoodLogRequest.

    One of ``scan_id`` / ``dish_name`` is required; both is allowed
    (scan_id wins in the service). Validation runs cheap field checks
    here; ownership and dish-resolvability are checked downstream by
    FoodLogService.
    """

    scan_id = serializers.UUIDField(required=False)
    dish_name = serializers.CharField(
        required=False, allow_blank=False, max_length=200,
    )
    portion_multiplier = serializers.FloatField(min_value=0.1, max_value=20.0)
    meal_type = serializers.ChoiceField(choices=FoodLog.MealType.choices)
    logged_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        if not attrs.get("scan_id") and not attrs.get("dish_name"):
            raise serializers.ValidationError(
                "Either scan_id or dish_name must be provided."
            )
        return attrs


class FoodLogEntrySerializer(serializers.ModelSerializer):
    """Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION FoodLogEntry."""

    class Meta:
        model = FoodLog
        fields = [
            "id",
            "dish_name",
            "calories",
            "protein_g",
            "fat_g",
            "carbs_g",
            "meal_type",
            "logged_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Daily summary (Slice 3c)
# ---------------------------------------------------------------------------


class NutritionSummaryQuerySerializer(serializers.Serializer):
    """Query string for GET /nutrition/summary/."""

    date = serializers.DateField(required=False, format="%Y-%m-%d")


class NutritionSummaryResponseSerializer(serializers.Serializer):
    """Response shape per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION
    NutritionSummaryResponse.

    Stubbed fields: ``water_ml`` (0) and ``water_goal_ml`` (settings
    default) until Slice 4; ``vitamin_deficits`` ({}) until Slice 3a'.
    """

    date = serializers.DateField(format="%Y-%m-%d")
    calories_total = serializers.FloatField(source="totals.calories")
    calories_goal = serializers.IntegerField()
    protein_g = serializers.FloatField(source="totals.protein_g")
    fat_g = serializers.FloatField(source="totals.fat_g")
    carbs_g = serializers.FloatField(source="totals.carbs_g")
    water_ml = serializers.IntegerField()
    water_goal_ml = serializers.IntegerField()
    entries = FoodLogEntrySerializer(many=True)
    vitamin_deficits = serializers.DictField(
        child=serializers.FloatField(),
    )


# ---------------------------------------------------------------------------
# Water tracker (Slice 4)
# ---------------------------------------------------------------------------


# Spec v2.0 §FOOD SCANNER+NUTRITION: 150 | 200 | 250 | 350 | 500.
# Free-form amounts are out of scope (mobile UI is fixed buttons).
WATER_AMOUNT_CHOICES = (150, 200, 250, 350, 500)


class WaterLogCreateSerializer(serializers.Serializer):
    """POST /nutrition/water — fixed-amount glass entry."""

    amount_ml = serializers.IntegerField()

    def validate_amount_ml(self, value: int) -> int:
        if value not in WATER_AMOUNT_CHOICES:
            raise serializers.ValidationError(
                f"amount_ml must be one of {sorted(WATER_AMOUNT_CHOICES)}"
            )
        return value


class WaterLogResponseSerializer(serializers.Serializer):
    """POST/DELETE response — aggregate + the affected log id.

    Per spec ``WaterLogResponse``: water_ml, water_goal_ml, water_pct,
    log_id. Source is a ``WaterLogCreatedResponse`` dataclass so the
    view doesn't need to assemble dicts inline.
    """

    water_ml = serializers.IntegerField(source="aggregate.water_ml")
    water_goal_ml = serializers.IntegerField(source="aggregate.water_goal_ml")
    water_pct = serializers.IntegerField(source="aggregate.water_pct")
    log_id = serializers.UUIDField()


class WaterTodayLogSerializer(serializers.ModelSerializer):
    """One row in WaterTodayResponse.logs[]."""

    class Meta:
        model = WaterLog
        fields = ["id", "amount_ml", "logged_at"]
        read_only_fields = fields


class WaterTodayResponseSerializer(serializers.Serializer):
    """GET /water/today response shape per spec WaterTodayResponse."""

    logs = WaterTodayLogSerializer(many=True)
    water_ml = serializers.IntegerField(source="aggregate.water_ml")
    water_goal_ml = serializers.IntegerField(source="aggregate.water_goal_ml")
