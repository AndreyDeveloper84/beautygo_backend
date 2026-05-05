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

    class MicronutrientSource(models.TextChoices):
        # Track E (DRF-260): which source filled the row's vitamins/minerals.
        # Pattern engine (DRF-262/309) skips windows where >40% of entries
        # are AI-estimated, because hallucinated micronutrients become
        # bogus deficit signals — and those drive cross-domain rules.
        USDA_FULL = "usda_full", "USDA full data"
        USDA_PARTIAL = "usda_partial", "USDA partial"
        ROSPOTREBNADZOR = "rospotrebnadzor", "Скурихин-Тутельян"
        AI_ESTIMATE = "ai_estimate", "Estimated by LLM"
        UNKNOWN = "unknown", "No data"

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

    # DRF-260: Track E micronutrient snapshot.
    # All nullable for backwards compatibility — existing logs stay valid
    # with all eight set to None and ``micronutrients_source="unknown"``.
    # The cross-domain rule engine (Track E) skips windows where >40% of
    # entries are AI-estimated, so the source field is load-bearing.
    vitamin_d_iu = models.FloatField(null=True, blank=True)
    vitamin_b12_mcg = models.FloatField(null=True, blank=True)
    vitamin_c_mg = models.FloatField(null=True, blank=True)
    iron_mg = models.FloatField(null=True, blank=True)
    calcium_mg = models.FloatField(null=True, blank=True)
    magnesium_mg = models.FloatField(null=True, blank=True)
    omega3_g = models.FloatField(null=True, blank=True)
    fiber_g = models.FloatField(null=True, blank=True)
    micronutrients_source = models.CharField(
        max_length=20,
        choices=MicronutrientSource.choices,
        default=MicronutrientSource.UNKNOWN,
        help_text="Where the row's vitamins/minerals came from. "
                  "Track E pattern engine reads this to filter low-quality data.",
    )

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


class NutritionOutboxEvent(models.Model):
    """Transactional outbox for cross-system webhooks (DRF-306).

    Spec: docs/plans/maxbot-phase3-ayla-spec.md §5.1 + linear-issues §DRF-306.

    Domain code writes ``NutritionOutboxEvent`` rows in the same DB
    transaction as the change that emitted them (profile upsert, water
    log, milestone reach, scan recognise, pattern fire). The Celery
    delivery worker polls pending rows, POSTs them to the receiver
    (MAX bot's webhook endpoint) with an HMAC-SHA256 signature header,
    and runs an exponential-backoff retry ladder. After
    ``MAX_RETRIES`` failed attempts the row moves to DLQ and an admin
    alert is logged.

    Independent of ``appointments.OutboxEvent`` — that one carries
    booking-domain topics for in-process handlers, this one carries
    nutrition-domain topics for an HTTP receiver.
    """

    class Topic(models.TextChoices):
        PROFILE_UPDATED = "profile_updated", "Профиль обновлён"
        WATER_LOGGED = "water_logged", "Записан напиток"
        MILESTONE_REACHED = "milestone_reached", "Milestone достигнут"
        PATTERN_DETECTED = "pattern_detected", "Паттерн обнаружен"
        RECOGNITION_COMPLETED = "recognition_completed", "Распознавание завершено"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает доставки"
        DELIVERED = "delivered", "Доставлено"
        DLQ = "dlq", "Dead-letter (не доставлено)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=32, choices=Topic.choices)
    external_user_id = models.CharField(
        max_length=64,
        help_text="bot:NNN form — receiver scopes incoming events by this.",
    )
    payload = models.JSONField(default=dict)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Nutrition Outbox Event"
        verbose_name_plural = "Nutrition Outbox Events"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "next_retry_at"]),
            models.Index(fields=["topic", "external_user_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.topic} → {self.external_user_id} ({self.status})"


class NutritionProfile(models.Model):
    """User nutrition profile (DRF-300).

    Spec: docs/plans/maxbot-phase3-ayla-spec.md §1.

    1:1 with ``settings.AUTH_USER_MODEL``. The bot reads this on every
    user message to render health-aware system-prompt cues; the mobile
    client reads it for the Phase 3 onboarding screen and the daily
    summary's "учла важное" panel.

    Stored norms (``daily_kcal``, ``daily_protein_g``, ...) are the
    post-override values returned by GET /profile/. The pre-override
    breakdown lives in ``last_overrides_applied`` JSON so the bot can
    explain *why* a goal got coerced (pregnancy/breastfeeding/BMR floor/
    eating_disorder ladder).

    All recalculation is pure-math (Mifflin-St Jeor + activity ×
    goal_factor + override ladder). No LLM, no external API.
    """

    class Gender(models.TextChoices):
        FEMALE = "female", "Женский"
        MALE = "male", "Мужской"

    class Goal(models.TextChoices):
        LOSE = "lose", "Похудеть"
        MAINTAIN = "maintain", "Поддерживать"
        GAIN = "gain", "Набрать"
        TONE = "tone", "Подтянуть"

    class Pace(models.TextChoices):
        GENTLE = "gentle", "Мягкий"
        MODERATE = "moderate", "Средний"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="nutrition_profile",
        primary_key=True,
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="nutrition_profiles",
    )

    # Anthropometry
    gender = models.CharField(
        max_length=8, choices=Gender.choices, blank=True, default="",
    )
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    height_cm = models.PositiveSmallIntegerField(null=True, blank=True)
    weight_kg = models.FloatField(null=True, blank=True)
    weight_range = models.CharField(
        max_length=16, blank=True, default="",
        help_text='"65-75" если ввели только диапазон.',
    )
    timezone = models.CharField(max_length=64, blank=True, default="UTC")

    # Goals
    activity_coefficient = models.FloatField(default=1.4)
    goal = models.CharField(
        max_length=12, choices=Goal.choices, blank=True, default="",
    )
    pace = models.CharField(
        max_length=12, choices=Pace.choices, blank=True, default="",
    )
    diet_preference = models.CharField(max_length=32, blank=True, default="none")

    # Health flags + skipped markers + allergies
    health_flags = models.JSONField(default=dict, blank=True)

    # Computed norms (post-override)
    bmr = models.PositiveIntegerField(default=0)
    daily_kcal = models.PositiveIntegerField(default=0)
    daily_protein_g = models.PositiveSmallIntegerField(default=0)
    daily_fat_g = models.PositiveSmallIntegerField(default=0)
    daily_carbs_g = models.PositiveSmallIntegerField(default=0)
    daily_water_ml = models.PositiveIntegerField(default=0)

    # Override audit
    goal_overridden_by = models.CharField(max_length=24, blank=True, default="")
    bmi_warning_overridden_at = models.DateTimeField(null=True, blank=True)
    last_overrides_applied = models.JSONField(default=list, blank=True)

    # Disclaimer ack {ts, version, screen}
    disclaimer_acked = models.JSONField(null=True, blank=True)

    # Lifecycle markers
    onboarded_at = models.DateTimeField(null=True, blank=True)
    first_food_logged_at = models.DateTimeField(null=True, blank=True)
    weekly_summary_unlocked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Nutrition Profile"
        verbose_name_plural = "Nutrition Profiles"

    def __str__(self) -> str:
        return f"NutritionProfile(user={self.user_id}, goal={self.goal or '?'})"


class ProfileIdempotencyKey(models.Model):
    """24-hour replay cache for POST /internal/profile/ (DRF-300).

    Stores the (key, response) tuple per user so a retried upsert with
    the same Idempotency-Key returns the cached response and never
    double-applies the patch. ``ttl`` cleanup is a cheap delete by
    expires_at — there's no Celery beat for it; the next write past TTL
    overwrites in place.
    """

    key = models.CharField(max_length=128, primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile_idempotency_keys",
    )
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["expires_at"])]


class WaterEntry(models.Model):
    """One row per drink the user logs through the Phase 3 tracker (DRF-302).

    Spec: docs/plans/maxbot-phase3-ayla-spec.md §2 (POST/DELETE/GET /water/).

    Distinct from ``WaterLog`` (the Slice 4 fixed-button mobile entry) —
    ``WaterEntry`` is bot-driven, supports any beverage (coffee, kefir,
    beer), tracks macros and caffeine, and supports soft-delete + restore
    within a 15-minute window. The two coexist until WaterLog can be
    folded in (Phase 3 cleanup pass).

    Macros (kcal/protein/...) are *snapshotted* at log time via
    ``Beverage × ml/100`` so future catalog corrections don't retroactively
    change yesterday's diary. This mirrors the FoodLog snapshot policy.

    Soft-delete via ``deleted_at`` + ``deleted_reason``. The linked
    ``food_log`` is hard-deleted on soft-delete and re-created from the
    snapshot fields on restore — a bot-only constraint, the FoodLog
    table itself doesn't need a soft-delete column.
    """

    class DeletedReason(models.TextChoices):
        USER_UNDO = "user_undo", "Пользователь отменил"
        ADMIN = "admin", "Удалено администратором"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="water_entries",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="water_entries",
    )
    beverage = models.ForeignKey(
        "Beverage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
        help_text="Null = pure water entry (тапнули кнопку без выбора напитка).",
    )

    ts = models.DateTimeField()
    ml = models.PositiveIntegerField()

    # Hydration delivered. Float because water_coefficient can be 1.50 (milk)
    # or negative (vodka). Aggregation in /water/today clamps the daily
    # total at 0; here we store raw physiology.
    water_ml = models.FloatField()

    kcal = models.FloatField(default=0.0)
    protein_g = models.FloatField(default=0.0)
    fat_g = models.FloatField(default=0.0)
    carbs_g = models.FloatField(default=0.0)
    sugar_g = models.FloatField(default=0.0)
    caffeine_mg = models.FloatField(default=0.0)

    # DRF-260: Track E micronutrient snapshot from Beverage.* per-100ml ×
    # ml/100. All nullable; only juices/milk/broth carry meaningful values.
    vitamin_d_iu = models.FloatField(null=True, blank=True)
    vitamin_b12_mcg = models.FloatField(null=True, blank=True)
    vitamin_c_mg = models.FloatField(null=True, blank=True)
    iron_mg = models.FloatField(null=True, blank=True)
    calcium_mg = models.FloatField(null=True, blank=True)
    magnesium_mg = models.FloatField(null=True, blank=True)
    omega3_g = models.FloatField(null=True, blank=True)
    fiber_g = models.FloatField(null=True, blank=True)

    # When kcal>0 we mirror into FoodLog so /summary/ daily totals stay
    # accurate. SET_NULL because we hard-delete the FoodLog on undo and
    # re-create on restore — see WaterEntryService.
    food_log = models.ForeignKey(
        FoodLog,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="water_entries",
    )

    # 50 / 100 / 150 — set once when this entry crossed the threshold for
    # the day; null otherwise. Per-day per-threshold idempotency rule
    # (spec §2.1) is enforced by WaterEntryService at write time, not by
    # a unique constraint, because soft-deleting a milestone entry should
    # NOT free the threshold up (the user already saw the message).
    milestone_threshold = models.PositiveSmallIntegerField(null=True, blank=True)

    idempotency_key = models.CharField(
        max_length=100, unique=True, null=True, blank=True,
        help_text="UUID5(user, ts, ml, slug) — POST /water/ dedup.",
    )

    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_reason = models.CharField(
        max_length=24, choices=DeletedReason.choices, blank=True, default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Water Entry"
        verbose_name_plural = "Water Entries"
        ordering = ["-ts"]
        indexes = [
            models.Index(fields=["user", "-ts"]),
            models.Index(fields=["user", "deleted_at"]),
        ]

    def __str__(self) -> str:
        bev = self.beverage.slug if self.beverage_id else "water"
        return f"{self.ml}ml {bev} @ {self.ts:%Y-%m-%d %H:%M}"


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

    # DRF-260: Track E micronutrients per 100ml. Most water/tea rows leave
    # these null; juices/milk/broth populate them from USDA + Скурихин.
    vitamin_d_iu_per_100ml = models.FloatField(null=True, blank=True)
    vitamin_b12_mcg_per_100ml = models.FloatField(null=True, blank=True)
    vitamin_c_mg_per_100ml = models.FloatField(null=True, blank=True)
    iron_mg_per_100ml = models.FloatField(null=True, blank=True)
    calcium_mg_per_100ml = models.FloatField(null=True, blank=True)
    magnesium_mg_per_100ml = models.FloatField(null=True, blank=True)
    omega3_g_per_100ml = models.FloatField(null=True, blank=True)
    fiber_g_per_100ml = models.FloatField(null=True, blank=True)

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


# ---------------------------------------------------------------------------
# DRF-263: Track E cross-domain rule engine
# ---------------------------------------------------------------------------


class CrossDomainRule(models.Model):
    """Catalogue row mapping a nutrition trigger to a beauty service.

    The cross-domain rule engine evaluates these rules against a user's
    active patterns (DRF-262) and surfaces a top-1 recommendation. Each
    rule has its own cooldown windows (PO-approved 2026-05-05: hybrid
    30/14/7d) so we can tune per-rule based on engagement data.

    Activation gate: ``is_active=True`` AND ``legal_reviewed=True`` —
    both must be true. Default is False on both, so a rule lands
    inert until a curator reviews and flips them. This is a hard
    requirement under the «Реклама медицинских услуг» constraint
    (CLAUDE.md «Запрещённые действия»).
    """

    SURFACE_BOT = "bot"
    SURFACE_MOBILE = "mobile"

    rule_id = models.SlugField(max_length=128, unique=True)
    nutrition_trigger = models.CharField(
        max_length=64,
        help_text="Pattern slug from detect_patterns (e.g. low_vitamin_d).",
    )
    service_category_slug = models.CharField(
        max_length=64,
        help_text="ServiceCategory.slug the rule recommends.",
    )
    service_modifier = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Optional sub-tag like 'argan_oil' for matching.",
    )

    insight_text_template = models.TextField()
    rationale_text = models.TextField()
    disclaimer_text = models.TextField()

    min_data_points = models.PositiveSmallIntegerField(
        default=3,
        help_text="Pattern must have ≥N data points before we activate.",
    )

    # Cooldown windows (PO-approved 2026-05-05).
    cooldown_rule_days = models.PositiveSmallIntegerField(default=30)
    cooldown_trigger_days = models.PositiveSmallIntegerField(default=14)
    cooldown_category_days = models.PositiveSmallIntegerField(default=7)
    skip_extend_days = models.PositiveSmallIntegerField(default=7)
    double_skip_pause_days = models.PositiveSmallIntegerField(default=60)

    # Health-flag exclusions, e.g. ["pregnant", "eating_disorder"].
    excluded_health_flags = models.JSONField(default=list, blank=True)

    # Activation gates — both must be True for the engine to use the rule.
    legal_reviewed = models.BooleanField(default=False)
    legal_review_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=False)

    # Premium gating (PO-approved 2026-05-05: ships False, toggle in admin).
    requires_premium = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cross-Domain Rule"
        verbose_name_plural = "Cross-Domain Rules"
        indexes = [
            models.Index(fields=["nutrition_trigger", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.rule_id} ({self.nutrition_trigger}→{self.service_category_slug})"


class CrossDomainShownRule(models.Model):
    """History of cross-domain recommendations served to a user (DRF-263).

    The cooldown engine reads these rows to decide whether a rule is
    eligible. Rows are immutable except for ``user_action`` and
    ``seen_at`` / ``appointment`` (set on /seen/ and /convert/).

    Auto-confirm-5min: a row counts as "seen" when ``seen_at`` is non-
    null OR ``shown_at + 5 minutes`` has passed. Defends against client
    crashes between the engine GET and the surface's explicit POST /seen/.
    """

    SURFACE_CHOICES = (
        ("bot", "MAX Bot"),
        ("mobile", "Mobile app"),  # reserved for Phase 5+
    )

    USER_ACTION_CHOICES = (
        ("none", "No action"),
        ("dismissed", "Dismissed"),
        ("paused_7d", "Paused 7 days"),
        ("converted", "Converted to booking"),
        ("explained", "Asked Откуда ты знаешь"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cross_domain_shown",
    )
    rule = models.ForeignKey(
        CrossDomainRule,
        on_delete=models.PROTECT,
        related_name="shown_rules",
    )

    # Snapshot at show time so cooldown lookups don't have to JOIN
    # CrossDomainRule (and so a rule rename never breaks attribution).
    nutrition_trigger = models.CharField(max_length=64)
    service_category_slug = models.CharField(max_length=64)

    shown_at = models.DateTimeField()
    seen_at = models.DateTimeField(null=True, blank=True)
    surface = models.CharField(max_length=12, choices=SURFACE_CHOICES)
    user_action = models.CharField(
        max_length=16, choices=USER_ACTION_CHOICES, default="none",
    )

    # Conversion attribution — set on POST /convert/ when the user
    # actually books. DRF-263 LB-8: SET_NULL preserves the attribution
    # row even when the appointment is deleted (GDPR purges, admin
    # cleanups, test fixtures). PROTECT would block the appointment
    # delete itself — wrong for an analytics row that must outlive the
    # booking it once tracked.
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="cross_domain_attributions",
    )

    class Meta:
        verbose_name = "Cross-Domain Shown Rule"
        verbose_name_plural = "Cross-Domain Shown Rules"
        ordering = ["-shown_at"]
        indexes = [
            # Cooldown queries — sub-ms reads needed.
            models.Index(fields=["user", "-shown_at"]),
            models.Index(fields=["user", "rule"]),
            models.Index(fields=["user", "nutrition_trigger"]),
            models.Index(fields=["user", "service_category_slug"]),
        ]

    def __str__(self) -> str:
        return f"{self.rule.rule_id} → user {self.user_id} ({self.user_action})"
