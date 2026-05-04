"""DRF serializers for nutrition endpoints.

Slice 2 ships POST /scan/ — the request takes multipart/form-data with
the image; the response shape mirrors the spec's ``FoodScanResponse``.
"""
from __future__ import annotations

from rest_framework import serializers

from nutrition.models import Beverage, FoodLog, FoodScan, NutritionProfile, WaterLog


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

        Returns null when:
        - The seed/lookup missed (no internal JSON), OR
        - The lookup matched a dish but the provider didn't estimate
          portion size, leaving per-portion totals null.

        In both cases mobile prompts manual entry. Vitamins is an empty
        map for now — seed has no vitamin data; Slice 3a'' OFF/USDA
        lookup will populate.
        """
        n = obj.nutrition
        if not n:
            return None
        kcal = n.get("kcal")
        protein = n.get("protein_g")
        fat = n.get("fat_g")
        carbs = n.get("carbs_g")
        if all(v is None for v in (kcal, protein, fat, carbs)):
            return None
        return {
            "calories": kcal,
            "protein_g": protein,
            "fat_g": fat,
            "carbs_g": carbs,
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


# ---------------------------------------------------------------------------
# Nutrition profile — Phase 3 (DRF-300)
# ---------------------------------------------------------------------------


_HEALTH_FLAG_KEYS = {
    "pregnant", "breastfeeding",
    "diabetes_t1", "diabetes_t2", "prediabetes",
    "hypertension", "gi_problems", "thyroid", "menopause",
    "eating_disorder", "meds",
    "allergies", "allergies_vague",
    "gender_skipped", "age_skipped", "height_skipped", "weight_skipped",
}


class DisclaimerAckSerializer(serializers.Serializer):
    ts = serializers.DateTimeField()
    version = serializers.CharField(max_length=16)
    screen = serializers.CharField(max_length=64)


class NutritionProfileUpsertSerializer(serializers.Serializer):
    """POST /internal/profile/ request — every field optional (PATCH semantics).

    Validation per spec §1.2: age 16-99, height 120-220, weight 30-200.
    """

    gender = serializers.ChoiceField(
        choices=NutritionProfile.Gender.choices, required=False, allow_blank=True,
    )
    age = serializers.IntegerField(min_value=16, max_value=99, required=False)
    height_cm = serializers.IntegerField(
        min_value=120, max_value=220, required=False,
    )
    weight_kg = serializers.FloatField(
        min_value=30, max_value=200, required=False,
    )
    weight_range = serializers.CharField(required=False, allow_blank=True, max_length=16)
    activity_coefficient = serializers.FloatField(
        min_value=1.0, max_value=2.5, required=False,
    )
    goal = serializers.ChoiceField(
        choices=NutritionProfile.Goal.choices, required=False, allow_blank=True,
    )
    pace = serializers.ChoiceField(
        choices=NutritionProfile.Pace.choices, required=False, allow_blank=True,
    )
    diet_preference = serializers.CharField(
        required=False, allow_blank=True, max_length=32,
    )
    timezone = serializers.CharField(required=False, allow_blank=True, max_length=64)
    health_flags = serializers.DictField(required=False)
    _skipped_fields = serializers.ListField(
        child=serializers.CharField(), required=False,
    )
    disclaimer_acked = DisclaimerAckSerializer(required=False)
    complete = serializers.BooleanField(required=False, default=False)

    def validate_health_flags(self, value: dict) -> dict:
        unknown = set(value.keys()) - _HEALTH_FLAG_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"Unknown health_flags keys: {sorted(unknown)}"
            )
        return value


class NutritionProfileResponseSerializer(serializers.Serializer):
    """Wire shape of GET / POST /internal/profile/ (spec §1.1)."""

    external_user_id = serializers.CharField()
    exists = serializers.BooleanField()

    gender = serializers.CharField(allow_null=True, allow_blank=True)
    age = serializers.IntegerField(allow_null=True)
    height_cm = serializers.IntegerField(allow_null=True)
    weight_kg = serializers.FloatField(allow_null=True)
    weight_range = serializers.CharField(allow_null=True, allow_blank=True)
    activity_coefficient = serializers.FloatField()
    goal = serializers.CharField(allow_null=True, allow_blank=True)
    pace = serializers.CharField(allow_null=True, allow_blank=True)
    diet_preference = serializers.CharField()

    norms = serializers.DictField(child=serializers.IntegerField())
    health_flags = serializers.DictField()

    goal_overridden_by = serializers.CharField(allow_null=True, allow_blank=True)
    bmi_warning_overridden_at = serializers.DateTimeField(allow_null=True)
    overrides_applied = serializers.ListField(child=serializers.DictField())

    disclaimer_acked = serializers.JSONField(allow_null=True)
    onboarded_at = serializers.DateTimeField(allow_null=True)
    first_food_logged_at = serializers.DateTimeField(allow_null=True)
    weekly_summary_unlocked_at = serializers.DateTimeField(allow_null=True)

    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# Water tracker — Phase 3 (DRF-302)
# ---------------------------------------------------------------------------


class WaterEntryCreateSerializer(serializers.Serializer):
    """POST /internal/water/ request body (spec §2.1)."""

    ml = serializers.IntegerField(min_value=10, max_value=3000)
    beverage_slug = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=64,
    )
    ts = serializers.DateTimeField(required=False)


class WaterEntryResponseSerializer(serializers.Serializer):
    """POST /internal/water/ response shape (spec §2.1)."""

    entry_id = serializers.UUIDField()
    ml = serializers.IntegerField()
    water_ml = serializers.IntegerField()
    beverage_name = serializers.CharField(allow_null=True)
    beverage_label = serializers.CharField(allow_null=True)
    kcal = serializers.FloatField()
    protein_g = serializers.FloatField()
    fat_g = serializers.FloatField()
    carbs_g = serializers.FloatField()
    caffeine_mg = serializers.FloatField()
    today_total_water_ml = serializers.IntegerField()
    today_norm_water_ml = serializers.IntegerField()
    today_progress_pct = serializers.IntegerField()
    milestone_text = serializers.CharField(allow_null=True)
    alcohol_recovery_hint = serializers.BooleanField()
    caffeine_warning = serializers.CharField(allow_null=True)
    ts = serializers.DateTimeField()


class WaterTodayEntrySerializer(serializers.Serializer):
    entry_id = serializers.UUIDField()
    ts = serializers.DateTimeField()
    ml = serializers.IntegerField()
    water_ml = serializers.IntegerField()
    beverage_slug = serializers.CharField(allow_null=True)
    beverage_name = serializers.CharField(allow_null=True)
    deleted = serializers.BooleanField()


class WaterTodayResponseSerializerV3(serializers.Serializer):
    """GET /internal/water/today/ (spec §2.4) — distinct from the Slice 4
    WaterTodayResponseSerializer above (mobile fixed-button glasses)."""

    date = serializers.DateField(format="%Y-%m-%d")
    entries = WaterTodayEntrySerializer(many=True)
    today_total_water_ml = serializers.IntegerField()
    today_norm_water_ml = serializers.IntegerField()
    today_kcal_from_beverages = serializers.FloatField()
    today_caffeine_mg = serializers.FloatField()
    today_total_coffee_cups = serializers.IntegerField()
    today_total_tea_cups = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Beverage catalog (DRF-301)
# ---------------------------------------------------------------------------


class BeverageCatalogItemSerializer(serializers.ModelSerializer):
    """Single row in GET /internal/beverages/.

    Spec §2.5: only UI metadata is exposed — the bot doesn't need the
    macro coefficients, those are applied server-side by POST /water/.
    """

    class Meta:
        model = Beverage
        fields = [
            "slug",
            "name_ru",
            "category",
            "aliases",
            "default_serving_ml",
            "default_serving_label",
        ]
        read_only_fields = fields
