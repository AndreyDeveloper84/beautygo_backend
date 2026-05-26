import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils import timezone


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ROLE_CHOICES = (
        ('client', 'Client'),
        ('specialist', 'Specialist'),
        ('admin', 'Admin'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    is_guest = models.BooleanField(
        default=False,
        help_text="Anonymous (guest) user created on first app launch without registration",
    )
    is_proxy = models.BooleanField(
        default=False,
        help_text=(
            "Proxy user created via service-to-service auth (e.g. MAX bot calling "
            "Ayla nutrition API on behalf of a BotUser). Username is namespaced "
            "(e.g. 'bot:12345'). Phase C migration links proxy to a real account."
        ),
    )
    onboarding_completed = models.BooleanField(
        default=False,
        help_text="User has completed the onboarding flow (name + location saved)",
    )
    # tenant FK — DRF-242.3. null=True for legacy / single-tenant rows
    # (no backfill yet; that lives in DRF-242.4 with the feature flag).
    # PROTECT because dropping a tenant must not orphan its users —
    # admin must reassign / soft-delete first.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta(AbstractUser.Meta):
        # Composite indexes — DRF-242.7. Tenant-first scoping is the
        # dominant access pattern: every list endpoint filters by
        # request.tenant before any other predicate, so the index
        # ordering matches the WHERE clause prefix.
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="user_tenant_active_idx",
            ),
            models.Index(
                fields=["tenant", "role"],
                name="user_tenant_role_idx",
            ),
        ]

    def __str__(self):
        return f"{self.username} ({self.role})"

    @property
    def is_client(self) -> bool:
        return self.role == 'client'

    @property
    def is_specialist(self) -> bool:
        return self.role == 'specialist'

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'


class Profile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )

    full_name = models.CharField(max_length=255)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    bio = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    experience_years = models.PositiveIntegerField(default=0)
    default_location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )
    default_location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )

    def __str__(self):
        return f"Профиль {self.user.username}"


class SpecialistProfile(models.Model):
    """Profile for specialists (masters) in BeautyGO Pro."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class ProfileStatus(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PENDING = "pending", "На верификации"
        ACTIVE = "active", "Активен"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="specialist_profile",
    )
    # tenant FK — DRF-242.3. Denormalized from user.tenant for query
    # performance — every Pro app queryset filters by tenant before
    # loading the user. null=True until 242.4 backfill.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="specialist_profiles",
    )
    display_name = models.CharField(max_length=255)
    avatar = models.ImageField(
        upload_to='specialists/avatars/', blank=True, null=True,
    )
    bio = models.TextField(blank=True)
    experience_years = models.PositiveIntegerField(default=0)

    # Location
    address = models.CharField(max_length=500, blank=True)
    location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )
    location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True,
    )

    # Booking engine fields
    timezone = models.CharField(
        max_length=50, default="Europe/Moscow",
        help_text="IANA timezone string, e.g. Europe/Moscow",
    )
    is_booking_enabled = models.BooleanField(
        default=True,
        help_text="Master can pause bookings without deactivating profile",
    )

    # Status & ratings
    status = models.CharField(
        max_length=10,
        choices=ProfileStatus.choices,
        default=ProfileStatus.DRAFT,
    )
    rating = models.DecimalField(
        max_digits=2, decimal_places=1, default=0.0,
    )
    reviews_count = models.PositiveIntegerField(default=0)
    is_available = models.BooleanField(default=True)

    # Booking-source dual mode (#439, ADR-0009 §Booking SoR rule).
    # 'ayla_local'  — Ayla djangoproject is the system of record. The
    #                 booking engine queries SpecialistWorkingHours /
    #                 SpecialistTimeOff in this DB and writes Appointment
    #                 rows here. No external API call.
    # 'yclients'    — YClients is the system of record. Ayla fetches
    #                 slots via the YClients API, creates the booking
    #                 in YClients, and mirrors the result locally for
    #                 the state machine. yclients_company_id is the
    #                 provider's YClients account id.
    # Default 'ayla_local' is current behaviour for every existing row
    # (this repo has no YClients integration yet — see #439).
    # TODO(phase-1.5): if multi-location providers appear, move this
    # field to a ProviderLocation table per the #439 issue body. For
    # MVP one source per specialist is enough.
    class BookingSource(models.TextChoices):
        AYLA_LOCAL = "ayla_local", "Ayla local DB SoR"
        YCLIENTS = "yclients", "YClients SoR"

    booking_source = models.CharField(
        max_length=20,
        choices=BookingSource.choices,
        default=BookingSource.AYLA_LOCAL,
    )
    yclients_company_id = models.CharField(
        max_length=64, blank=True, default="",
        help_text=(
            "YClients account/company id — required when "
            "booking_source='yclients', empty otherwise."
        ),
    )
    yclients_staff_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text=(
            "YClients per-master staff id — required when "
            "booking_source='yclients' and the admin mini-app needs "
            "to resolve YClients staff ids → SpecialistProfile rows. "
            "Empty otherwise."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Composite indexes — DRF-242.7. Pro-app dashboards filter by
        # tenant first, then by status (verification queue) or
        # is_available (active masters). Without these, the planner
        # falls back to a tenant-only scan plus app-side filter.
        indexes = [
            models.Index(
                fields=["tenant", "status"],
                name="sp_tenant_status_idx",
            ),
            models.Index(
                fields=["tenant", "is_available"],
                name="sp_tenant_available_idx",
            ),
        ]

    def __str__(self):
        return f"{self.display_name} ({self.get_status_display()})"


class SpecialistPortfolio(models.Model):
    """Portfolio image for a specialist (DRF-194).

    Public listing surfaces these on the specialist detail card; the
    Pro app manages them via /api/v1/specialists/me/portfolio/. We
    store the file in the same storage backend the rest of the project
    uses (django-storages → S3/Minio) and expose ``image.url`` to the
    API as ``image_url``. There's no separate ``s3_key`` field — the
    storage backend's ``image.name`` is the canonical key, and the
    storage handles deletion when the model row is removed.

    Per-specialist hard cap of 30 photos is enforced in the upload view,
    not in the model — keeping the cap in code lets us tune it without
    a migration round-trip and avoids the "constraint that needs the
    full row count" pattern PostgreSQL doesn't express cleanly.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        SpecialistProfile,
        on_delete=models.CASCADE,
        related_name='portfolio',
    )
    image = models.ImageField(upload_to='specialists/portfolio/')
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Stable ordering for the public listing. sort_order is the
        # specialist-controlled axis (PATCH endpoint); created_at is
        # the deterministic tiebreaker.
        ordering = ['sort_order', 'created_at']
        indexes = [
            models.Index(fields=['specialist', 'sort_order']),
        ]

    def __str__(self) -> str:
        return f"Portfolio #{self.id} of {self.specialist.display_name}"


class OTPCode(models.Model):
    """OTP code for phone verification."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone = models.CharField(max_length=20, db_index=True)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['phone', 'is_used', 'expires_at']),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired and self.attempts < settings.OTP_MAX_ATTEMPTS

    def __str__(self):
        return f"OTP for {self.phone} ({'used' if self.is_used else 'active'})"


class SocialAccount(models.Model):
    """Linked social provider account for a user."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Provider(models.TextChoices):
        VK = "vk", "VKontakte"
        GOOGLE = "google", "Google"
        APPLE = "apple", "Apple"
        YANDEX = "yandex", "Yandex"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='social_accounts',
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    provider_uid = models.CharField(max_length=255)
    extra_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('provider', 'provider_uid')
        indexes = [
            models.Index(fields=['user', 'provider']),
        ]

    def __str__(self):
        return f"{self.user.username} — {self.provider} ({self.provider_uid})"


class DeviceToken(models.Model):
    """FCM device token for push notifications."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class AppType(models.TextChoices):
        CLIENT = "client", "BeautyGO"
        PRO = "pro", "BeautyGO Pro"

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='device_tokens',
    )
    token = models.CharField(max_length=255, unique=True)
    app_type = models.CharField(max_length=10, choices=AppType.choices)
    platform = models.CharField(max_length=10, choices=Platform.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'app_type', 'is_active']),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.app_type}/{self.platform})"


class FavoriteSpecialist(models.Model):
    """A client's saved-favourite specialist (DRF-72).

    Per Notion API Spec v2.0 §FAVORITES:
        POST /favorites/specialists/{id}/   — add (idempotent)
        DELETE /favorites/specialists/{id}/ — remove (idempotent)
        GET /favorites/specialists/         — list

    Why a flat per-row model instead of a JSONField list on Profile:
    - DELETE / list / count queries stay indexable (one row per (user,
      specialist) pair).
    - Future "save reason" / "label" / "notification preference" fields
      get a natural home without migrating a JSON blob.
    - The home screen card on /api/v1/home/ orders favourites by
      ``-created_at`` to surface the most recently saved at the top —
      a JSONField list would be ambiguous on order.

    Cascade rules:
    - User deleted: rows go too — favourites belong to the user.
    - SpecialistProfile deleted: rows go too — keeping orphan favourites
      for a deleted master shows empty cards on the home screen.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorite_specialists",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        related_name="favorited_by",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Favorite Specialist"
        verbose_name_plural = "Favorite Specialists"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "specialist"],
                name="unique_favorite_per_user_specialist",
            ),
        ]
        indexes = [
            # Home screen and /favorites/specialists/ list queries.
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} ❤ {self.specialist_id}"


class UserPersonalContext(models.Model):
    """Minimum-lovable subset of AI personalization context (DRF-174 reduced).

    Per CEO scope reduction (project_m4_scope_reduction) the full personal
    context — life events, address tracking, behavioural inference, data
    provenance flags, sensitive-zone encryption — is deferred to Phase 6
    (post-pilot, when we have real usage patterns to drive design). This
    model keeps only the fields that:

    1. Mobile can collect through obvious UI prompts (Phase 3 onboarding
       + chat clarification questions).
    2. AI Chat can use as a system prompt hint without inferring anything.
    3. Don't need cross-table extraction logic.

    Schema decisions:

    - Fields use ``JSONField(default=list)`` for the multi-select cases —
      simple, no extra tables, easy to evolve. When the catalogue of
      valid values stabilises post-pilot, migrate to lookup tables.
    - All fields are optional (`blank=True` / `default=...`). The user
      builds context incrementally; absence of a value means "not asked
      yet" and AI Chat should treat it as no preference.
    - ``OneToOneField`` to User: every active user gets at most one
      context row, lazy-created on first PATCH (not via signal — avoids
      a row for every guest who never personalises).
    """

    class TimeSlot(models.TextChoices):
        EARLY_MORNING = "early_morning", "До 9 утра"
        MORNING = "morning", "Утро (9-12)"
        AFTERNOON = "afternoon", "День (12-17)"
        EVENING = "evening", "Вечер (17-21)"
        LATE_EVENING = "late_evening", "После 21"

    class DietType(models.TextChoices):
        OMNIVORE = "omnivore", "Всеядное"
        VEGETARIAN = "vegetarian", "Вегетарианство"
        VEGAN = "vegan", "Веганство"
        KETO = "keto", "Кето"
        HALAL = "halal", "Халяль"
        KOSHER = "kosher", "Кошер"
        OTHER = "other", "Другое"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_context",
    )

    # --- Location preferences ---
    preferred_districts = models.JSONField(
        default=list, blank=True,
        help_text="Список названий районов / станций метро где удобно "
                  "записываться. Mobile shows top-3, full list in profile.",
    )

    # --- Schedule preferences ---
    preferred_time_slots = models.JSONField(
        default=list, blank=True,
        help_text="Список ключей TimeSlot — какие окна удобнее. Validated "
                  "in serializer against TimeSlot choices.",
    )

    # --- Budget preferences ---
    price_range_min = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    price_range_max = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )

    # --- Dietary preference (for nutrition/food scanner — Phase 5+) ---
    diet_type = models.CharField(
        max_length=20, choices=DietType.choices, blank=True, default="",
    )

    # --- Health-relevant beauty preferences ---
    skin_sensitivities = models.JSONField(
        default=list, blank=True,
        help_text="Free-form list of allergens / sensitivities (no clinical "
                  "diagnosis — just user-stated preferences for vendors).",
    )

    # --- Behavioural preference ---
    prefers_flexible_cancellation = models.BooleanField(
        default=False,
        help_text="If True, AI Chat softly biases toward specialists with "
                  "lenient cancellation policies (Phase 6 hookup).",
    )

    # --- DRF-230 — North Star memory fields (added 2026-05-07) ---
    workplace_district = models.CharField(
        max_length=80, blank=True, default="",
        help_text="Free-form district / metro station near workplace.",
    )
    home_district = models.CharField(
        max_length=80, blank=True, default="",
        help_text="Free-form district / metro station near home.",
    )
    favorite_masters = models.JSONField(
        default=list, blank=True,
        help_text="List of SpecialistProfile.id strings rebooked >=3 times.",
    )
    min_rating_preference = models.FloatField(
        null=True, blank=True,
        help_text="Minimum specialist rating (0.0-5.0).",
    )
    busy_days = models.JSONField(
        default=list, blank=True,
        help_text="ISO weekday short names ('mon','tue',...) the user avoids.",
    )

    # --- DRF-230 service fields (PersonalizationEngine bookkeeping) ---
    skipped_questions = models.JSONField(
        default=dict, blank=True,
        help_text="Per-field {count:int, last_at:isoformat}. Skip x2 -> pause.",
    )
    last_asked_at = models.JSONField(
        default=dict, blank=True,
        help_text="Per-field last-asked timestamp for 24h cooldown.",
    )
    data_sources = models.JSONField(
        default=dict, blank=True,
        help_text="Per-field provenance: explicit / inferred / signal.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Personal Context"
        verbose_name_plural = "User Personal Contexts"

    def __str__(self) -> str:
        return f"PersonalContext for {self.user_id}"


class AnonymousSession(models.Model):
    """
    Tracks anonymous (guest) JWT sessions created before registration.

    One session per device_id. When the user signs up/logs in and provides
    the anonymous_token, the session is merged into the real account.
    """

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device_id = models.UUIDField(unique=True, db_index=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='anonymous_session',
    )
    platform = models.CharField(max_length=10, choices=Platform.choices)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Anonymous Session'
        verbose_name_plural = 'Anonymous Sessions'
        indexes = [
            models.Index(fields=['device_id']),
            models.Index(fields=['expires_at']),
        ]

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"AnonSession device={self.device_id} platform={self.platform}"


# ---------------------------------------------------------------------------
# TenantUserRelationship — multi-provider customer model (#246)
# ---------------------------------------------------------------------------

class TenantUserRelationship(models.Model):
    """A user's relationship with a tenant.

    Replaces the single ``User.tenant`` FK semantics for customers — a
    customer is multi-provider by default (massage at salon A + nails
    at salon B + freelance lash master + etc). Each link is permanent
    and equal-weight. There is no "primary tenant" for customers.

    Schema = variant β (partial unique + history) per founder ack 2026-05-24:
    each ``(user, tenant)`` pair can have AT MOST ONE row with
    ``is_active=True`` via a partial unique constraint. Inactive rows
    accumulate as history — every grant + revoke is a separate row.
    152-ФЗ audit requires "when did Tenant X have access to user Y" as
    a queryable fact (list of granted_at / revoked_at pairs), not a
    last-write-wins boolean.

    Role split (Q5 recommendation, founder ack pending):
    - ``customer`` — grants on first booking via Variant E. Default-
      deny + green-zone privacy across tenants.
    - ``staff`` — specialist working in a salon. Full employer data
      access. Multi-tenant for mobility (Q2: master moves salon).
    - ``admin`` — tenant owner / manager. Same as staff + admin perms.

    Staff-specific side-fields (hire_date, commission_rate, etc.)
    live on a separate ``StaffEmployment`` model when needed — keeps
    the customer-side TUR rows lean.
    """

    class Role(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        STAFF = "staff", "Staff"
        ADMIN = "admin", "Admin"

    class GrantedBy(models.TextChoices):
        SELF = "self", "Self (user-initiated)"
        ADMIN = "admin", "Admin (tenant-initiated)"
        SYSTEM = "system", "System (backfill / cascade)"

    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False,
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="tenant_relationships",
    )
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="user_relationships",
    )
    role = models.CharField(
        max_length=16, choices=Role.choices, default=Role.CUSTOMER,
        help_text=(
            "customer: granted on booking via Variant E. "
            "staff: specialist working in the salon. "
            "admin: tenant owner / manager."
        ),
    )
    is_active = models.BooleanField(default=True, db_index=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.CharField(
        max_length=16,
        choices=GrantedBy.choices,
        default=GrantedBy.SELF,
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
    )
    revoke_reason = models.CharField(
        max_length=128, blank=True, default="",
    )

    class Meta:
        verbose_name = "Tenant ↔ User relationship"
        verbose_name_plural = "Tenant ↔ User relationships"
        constraints = [
            # Partial unique: at most ONE active row per (user, tenant).
            # Inactive rows pile up as history. 152-ФЗ access-log support.
            models.UniqueConstraint(
                fields=["user", "tenant"],
                condition=models.Q(is_active=True),
                name="tur_unique_active",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "tenant"],
                name="tur_lookup_idx",
            ),
            models.Index(
                fields=["user", "is_active"],
                name="tur_user_active_idx",
            ),
        ]

    def __str__(self) -> str:
        flag = "active" if self.is_active else "revoked"
        return f"TUR({self.user_id}, {self.tenant_id}, {self.role}, {flag})"
