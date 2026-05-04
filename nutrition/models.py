"""Nutrition persistence — FoodScan one row per /scan/ request.

Stored regardless of provider success so we have:
- Audit trail (which provider, when, latency, raw response) for support
  disputes ("why did Ayla say my borscht had 800 kcal?")
- Cost attribution per user / per provider for the daily $-spend dashboard
- Replay for the "scan again" feature without re-uploading the photo
- Slice 3 nutrition lookup runs against this row, not against the wire
  response, so failed lookups can be retried offline

The image itself is in S3 (django-storages) — we keep just the storage
key on this row. Photo TTL on S3 is 30 days per docs/FOOD_SCANNER_DECISION.md
and is enforced at the bucket lifecycle level, not Django-side.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


def _scan_image_path(instance: "FoodScan", filename: str) -> str:
    return f"food-scans/{instance.user_id}/{instance.id}.jpg"


class FoodScan(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI Vision"
        YANDEX = "yandex", "Yandex Vision"
        VIT_SELF_HOST = "vit-self-host", "Self-host ViT"  # Phase 6

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="food_scans",
    )
    # tenant FK — DRF-242.3. Denormalized from user.tenant for query
    # performance: nutrition analytics queries scope by tenant first.
    # null=True until 242.4 backfill. PROTECT prevents orphan scans on
    # accidental tenant deletion.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="food_scans",
    )
    image = models.ImageField(upload_to=_scan_image_path)

    # Recognition result (from provider).
    dish_name = models.CharField(max_length=200, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    portion_g = models.FloatField(null=True, blank=True)
    ingredients = models.JSONField(default=list, blank=True)

    # Nutrition (filled by Slice 3 lookup; nullable for now).
    nutrition = models.JSONField(null=True, blank=True)

    # Provider audit.
    provider_used = models.CharField(
        max_length=24, choices=Provider.choices, blank=True, default="",
    )
    provider_fallback_from = models.CharField(
        max_length=24,
        blank=True,
        default="",
        help_text="If the primary provider failed, which one was tried first.",
    )
    latency_ms = models.IntegerField(default=0)
    raw_response = models.JSONField(default=dict, blank=True)

    # Failure tracking (when no provider returned a confident result).
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Food Scan"
        verbose_name_plural = "Food Scans"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["provider_used"]),
        ]

    def __str__(self) -> str:
        return f"{self.dish_name or '?'} ({self.provider_used}, conf={self.confidence:.2f})"


class FoodLog(models.Model):
    """User's food diary entry — one row per meal logged.

    Spec: Notion API Spec v2.0 §FOOD SCANNER + NUTRITION
          POST /nutrition/food-log → FoodLogEntry response.

    Two creation paths, both via the same POST /nutrition/food-log:
    - **scan_id path**: links to a prior FoodScan; macros are derived
      from FoodScan.nutrition × portion_multiplier
    - **manual path**: caller supplies ``dish_name``; macros are
      looked up via NutritionLookup against a 100g baseline ×
      portion_multiplier (see Slice 3b notes in services).

    Macros are **snapshot** at log time — if a seed entry or scan
    nutrition is later corrected, existing log rows are unaffected.
    The mobile diary stays stable.
    """

    class MealType(models.TextChoices):
        BREAKFAST = "breakfast", "Завтрак"
        LUNCH = "lunch", "Обед"
        DINNER = "dinner", "Ужин"
        SNACK = "snack", "Перекус"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="food_logs",
    )
    # Optional link to the scan this log came from (manual entries
    # leave it null). PROTECT would be wrong — if the user deletes a
    # scan we keep the log because the macros are already snapshotted.
    scan = models.ForeignKey(
        FoodScan,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="food_logs",
    )

    dish_name = models.CharField(max_length=200)
    portion_multiplier = models.FloatField(default=1.0)

    # Snapshotted macros — match the spec FoodLogEntry response shape.
    calories = models.FloatField(default=0.0)
    protein_g = models.FloatField(default=0.0)
    fat_g = models.FloatField(default=0.0)
    carbs_g = models.FloatField(default=0.0)

    meal_type = models.CharField(max_length=16, choices=MealType.choices)
    logged_at = models.DateTimeField()

    # Mobile retries on flaky network can double-POST the same meal —
    # caller passes X-Idempotency-Key header, we de-dup. Same pattern as
    # Appointment.idempotency_key (appointments/models.py). UUID-shaped
    # in practice; column-wide uniqueness is fine because UUIDs don't
    # collide across users.
    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True,
        help_text="Client-provided UUID for duplicate prevention.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Food Log"
        verbose_name_plural = "Food Logs"
        ordering = ["-logged_at"]
        indexes = [
            # Daily summary query: filter by user + logged_at date range.
            models.Index(fields=["user", "-logged_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.dish_name} ({self.meal_type}, {self.calories:.0f} kcal)"


class WaterLog(models.Model):
    """One row per glass of water the user tapped in the app.

    Spec: Notion API Spec v2.0 §FOOD SCANNER+NUTRITION
          POST /nutrition/water (amount_ml ∈ {150, 200, 250, 350, 500}),
          DELETE /nutrition/water/{id}, GET /nutrition/water/today.

    Why one row per glass instead of a single per-day counter:
    - Spec returns ``logs[]`` from /water/today for "undo last" UX
    - Mistakes happen ("oh I double-tapped 250") and DELETE needs an id
    - Future timeline / habit views read individual events naturally
    - Aggregation is a single SUM — cheap

    No ``portion_multiplier`` or unit conversion: amount is stored
    in millilitres, validated against the fixed set at the serializer
    layer. Free-form amounts are out of scope (spec is restrictive).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="water_logs",
    )
    amount_ml = models.PositiveIntegerField()
    logged_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Water Log"
        verbose_name_plural = "Water Logs"
        ordering = ["-logged_at"]
        indexes = [
            # Same shape as FoodLog — daily aggregate range scan.
            models.Index(fields=["user", "-logged_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.amount_ml}ml @ {self.logged_at:%Y-%m-%d %H:%M}"


class Beverage(models.Model):
    """Catalog row for the Phase 3 water/beverage tracker (DRF-301).

    Spec: docs/plans/maxbot-phase3-ayla-spec.md §2.5 + §8.

    Two consumers read this table:
    - GET /api/v1/nutrition/internal/beverages/ (autocomplete + free-text
      parser in the MAX bot — only UI metadata is exposed on the wire)
    - POST /api/v1/nutrition/internal/water/ (DRF-302) — uses
      water_coefficient + per-100ml macros to compute hydration and
      kcal/macros for the WaterEntry row.

    Sources for seed numbers:
    - USDA FoodData Central (kcal, protein, fat, carbs, sugar, caffeine)
    - Beverage Hydration Index, Maughan et al. 2016 — water_coefficient
    - Скурихин-Тутельян «Химический состав российских пищевых продуктов»
      for RU staples (бульон, ряженка, морс, квас).

    water_coefficient is signed: 1.0 = pure water; <1.0 mild diuretic
    (coffee, tea); negative for strong alcohol (net dehydration). The
    POST /water/ handler clamps the resulting water_ml at row level, not
    here — the catalog stores raw physiology.
    """

    class Category(models.TextChoices):
        WATER = "water", "Вода"
        TEA = "tea", "Чай"
        COFFEE = "coffee", "Кофе"
        JUICE = "juice", "Сок"
        SODA = "soda", "Газировка"
        MILK = "milk", "Молочное"
        ALCOHOL = "alcohol", "Алкоголь"
        BROTH = "broth", "Бульон"
        SPORT = "sport", "Спортивное"
        OTHER = "other", "Прочее"

    slug = models.SlugField(max_length=64, unique=True)
    name_ru = models.CharField(max_length=120)
    category = models.CharField(max_length=16, choices=Category.choices)

    water_coefficient = models.FloatField(
        help_text="Доля от объёма, идущая в гидратацию. 1.0 = вода. "
        "Может быть отрицательной для крепкого алкоголя.",
    )
    kcal_per_100ml = models.FloatField(default=0.0)
    protein_g_per_100ml = models.FloatField(default=0.0)
    fat_g_per_100ml = models.FloatField(default=0.0)
    carbs_g_per_100ml = models.FloatField(default=0.0)
    sugar_g_per_100ml = models.FloatField(default=0.0)
    caffeine_mg_per_100ml = models.FloatField(default=0.0)

    # Free-text aliases for parser ("кофе" / "coffee" / "americano" → kofe_chernyi).
    # JSONField list[str], lowercased on save (see save() override).
    aliases = models.JSONField(default=list, blank=True)

    default_serving_ml = models.PositiveIntegerField(default=250)
    default_serving_label = models.CharField(max_length=32, default="стакан")

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Beverage"
        verbose_name_plural = "Beverages"
        ordering = ["category", "name_ru"]
        indexes = [
            models.Index(fields=["category", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name_ru} ({self.category})"

    def save(self, *args, **kwargs):
        if self.aliases:
            self.aliases = [a.strip().lower() for a in self.aliases if a and a.strip()]
        super().save(*args, **kwargs)
