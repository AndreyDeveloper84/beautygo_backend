from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify


class ServiceCategory(models.Model):
    """Hierarchical category for beauty services."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='children',
    )
    icon = models.CharField(max_length=50, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # tenant FK — DRF-242.6. Categories are tenant-scoped because each
    # marketplace tenant curates its own taxonomy (a beauty salon's
    # "Маникюр" tree differs from a wellness studio's).
    # null=True for legacy / single-tenant rows; backfill in management
    # command. PROTECT to prevent silent loss of taxonomy on tenant
    # deletion — admin must reassign first.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="service_categories",
    )

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Service Categories'
        # Tenant-scoped taxonomy lookup — DRF-242.7.
        indexes = [
            models.Index(
                fields=['tenant', 'is_active'],
                name='svccat_tenant_active_idx',
            ),
        ]

    def clean(self) -> None:
        """Validate parent: no cycles, max depth 2 (root → subcategory)."""
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError(
                {'parent': 'Category cannot be its own parent.'}
            )
        if self.parent and self.parent.parent_id == self.pk:
            raise ValidationError(
                {'parent': 'Circular parent reference detected.'}
            )
        if self.parent_id and self.parent.parent_id is not None:
            raise ValidationError(
                {'parent': 'Category hierarchy cannot exceed 2 levels.'}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate slug from name and run validation before saving."""
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        if self.parent:
            return f"{self.parent.name} → {self.name}"
        return self.name


class ServiceTemplate(models.Model):
    """Предустановленный шаблон услуги, привязанный к категории.

    Используется на онбординге мастера, чтобы предложить готовый список
    популярных услуг с рекомендованной длительностью вместо пустой формы.
    Реальные цены вычисляются через `RegionalPricing` отдельно (DRF-197).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.CASCADE,
        related_name='templates',
    )
    name = models.CharField(max_length=100)
    name_short = models.CharField(
        max_length=40,
        help_text="Короткое имя для чипов/списков",
    )
    # Durations are nullable so the canonical catalog can be seeded from the
    # reference list before per-service timings are curated (see
    # seed_canonical_catalog + docs/CANONICAL_CATALOG_SEED_PLAN_2026-07.md).
    duration_default = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_min = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_max = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    # Canonical health-screening attributes (subset of #200 task 1). A gated
    # service must be screened for contraindications before booking; the bot
    # grounds its health-check on these via ayla_service_id.
    requires_health_check = models.BooleanField(
        default=False,
        help_text="Услуга требует проверки противопоказаний перед записью",
    )
    contraindications = models.TextField(
        blank=True, default="",
        help_text="Противопоказания / оговорки (мед. профиль, разрешение врача и т.п.)",
    )
    is_popular = models.BooleanField(
        default=False,
        help_text="Показывать в верхней части списка категории",
    )
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('category', 'name')]
        ordering = ['-is_popular', 'sort_order', 'name']
        indexes = [
            models.Index(fields=['category', 'is_popular', 'sort_order']),
        ]

    def clean(self) -> None:
        # Durations may be null on canonical rows that are not yet timed;
        # only cross-validate when the pair is present.
        if (
            self.duration_min is not None
            and self.duration_default is not None
            and self.duration_min > self.duration_default
        ):
            raise ValidationError(
                {'duration_min': 'duration_min must be <= duration_default.'}
            )
        if (
            self.duration_max is not None
            and self.duration_default is not None
            and self.duration_max < self.duration_default
        ):
            raise ValidationError(
                {'duration_max': 'duration_max must be >= duration_default.'}
            )

    def __str__(self) -> str:
        return f"{self.name} ({self.category.name})"


class RegionalPricing(models.Model):
    """Рекомендованные ценовые диапазоны для шаблона в конкретном регионе.

    Используется на онбординге мастера, чтобы показать реалистичную вилку.
    Регион определяется через `services.pricing.get_region_key()` по
    приоритету: city_input → reverse-geocoding по координатам → `default`.
    """

    DEFAULT_REGION_KEY = 'default'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        'ServiceTemplate',
        on_delete=models.CASCADE,
        related_name='regional_prices',
    )
    region_key = models.CharField(
        max_length=50,
        help_text="Нормализованный ключ региона: «penza», «moscow», «default»",
    )
    region_name = models.CharField(
        max_length=100,
        help_text="Человекочитаемое имя: «Пенза», «Москва»",
    )
    price_min = models.DecimalField(
        max_digits=8, decimal_places=0,
        validators=[MinValueValidator(1)],
    )
    price_max = models.DecimalField(
        max_digits=8, decimal_places=0,
        validators=[MinValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('template', 'region_key')]
        ordering = ['template', 'region_key']
        indexes = [
            models.Index(fields=['region_key']),
        ]

    def clean(self) -> None:
        if self.price_min > self.price_max:
            raise ValidationError(
                {'price_min': 'price_min must be <= price_max.'}
            )

    def __str__(self) -> str:
        return (
            f"{self.template.name} — {self.region_name} "
            f"({self.price_min:.0f}–{self.price_max:.0f} ₽)"
        )


class Service(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='services',
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='services',
    )
    # tenant FK — DRF-242.6. Denormalised from specialist.tenant so
    # marketplace listing queries can filter by tenant_id without JOIN
    # to SpecialistProfile. Invariant maintained by backfill + service
    # layer (see docs/MULTI_TENANT.md).
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="services",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(1)],
    )
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(15), MaxValueValidator(480)],
    )
    image = models.ImageField(
        upload_to='services/', blank=True, null=True,
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    buffer_after_minutes = models.PositiveSmallIntegerField(
        default=0,
        help_text="Required gap (minutes) after this service before next booking",
    )

    # B9 T+2h aftercare push body. Founder pilot safety rule
    # (project_pilot_scope_discipline): NO LLM-generated care advice
    # pilot — only approved canonical text per service. Empty value
    # is the default and suppresses the push entirely (explicit opt-in
    # per service when ops curator fills approved content via admin).
    # Sent VERBATIM by notifications.dispatch_post_visit_aftercare;
    # no template-side embellishment beyond a static title prefix.
    aftercare_text = models.TextField(
        blank=True, default='',
        help_text=(
            "Approved post-visit aftercare advice. Sent verbatim via "
            "B9 T+2h push. Empty (default) suppresses the push."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        # Composite indexes for the catalog filter patterns:
        # - filter by specialist + category + active state (catalog browse)
        # - filter by specialist + price range (price slider, "от-до")
        # Plain indexes on (specialist) and (category) alone aren't enough —
        # the planner needs combined coverage for the WHERE/AND chains the
        # catalog generates from query params.
        indexes = [
            models.Index(
                fields=['specialist', 'category', 'is_active'],
                name='svc_spec_cat_active_idx',
            ),
            models.Index(
                fields=['specialist', 'price'],
                name='svc_spec_price_idx',
            ),
            # Marketplace listing — DRF-242.7. Filter tenant first to
            # narrow the candidate set before any specialist/category
            # predicates kick in.
            models.Index(
                fields=['tenant', 'is_active'],
                name='svc_tenant_active_idx',
            ),
        ]

    def __str__(self):
        return f"{self.name} — {self.specialist.display_name}"


# --------------------------------------------------------------------------- #
# S3A canonical catalog rebuild (#1044 / #200) — additive new layer.
# ServiceTemplate (taxonomy) -> SalonService -> SpecialistService (bookable).
# Service / Appointment are intentionally NOT touched here (strangler-fig;
# cutover is the separate founder-authorized S3-CUT chunk). See
# docs/CATALOG_DOMAIN_REBUILD_S3_DESIGN_2026-07.md.
# --------------------------------------------------------------------------- #
class SalonService(models.Model):
    """A service a salon (Tenant) offers — mid layer of the catalog chain.

    Derived from a ``ServiceTemplate`` (taxonomy) or, for off-taxonomy
    custom offerings, standalone with an explicit category (D2). Not
    bookable on its own — a ``SpecialistService`` makes it bookable.
    """

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        YCLIENTS = "yclients", "YClients"
        SEED = "seed", "Seed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="salon_services",
    )
    # Nullable so off-taxonomy custom services are allowed (D2); when null,
    # ``category`` is required (enforced in clean()).
    template = models.ForeignKey(
        "ServiceTemplate",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="salon_services",
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="salon_services",
    )
    name = models.CharField(max_length=200)
    # Salon-level default; null resolves from template (see
    # SpecialistService.resolved_duration).
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    base_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        validators=[MinValueValidator(1)],
    )
    # Escalate-only floor vs template (D1): admin may set True on a salon
    # even when the template does not require it; cannot relax a gated
    # template downstream (SpecialistService.resolved_requires_health_check).
    requires_health_check = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.MANUAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "template", "name"],
                name="salonservice_tenant_template_name_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="salonsvc_tenant_active_idx",
            ),
            models.Index(
                fields=["tenant", "category", "is_active"],
                name="salonsvc_tenant_cat_active_idx",
            ),
        ]

    def clean(self) -> None:
        if self.template_id is None and self.category_id is None:
            raise ValidationError(
                {"category": "category is required for off-taxonomy custom services."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} @ {self.tenant.slug}"


class SpecialistService(models.Model):
    """A specialist performs a SalonService — the BOOKABLE catalog unit.

    ``id`` is the stable booking key the bot resolves (see the stable-id
    contract in the design doc). ``duration_minutes`` / ``requires_health_check``
    resolve down the chain (specialist -> salon -> template).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.PROTECT,
        related_name="specialist_services",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        related_name="specialist_services",
    )
    # Denormalized from salon_service for tenant-scoped queries; populated in
    # save() when unset. Nullable for parity with other denormalized tenant FKs.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="specialist_services",
    )
    # Nullable in DB; must be resolvable when is_active (enforced in clean()).
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(1)],
    )
    requires_health_check = models.BooleanField(default=False)
    buffer_after_minutes = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["specialist", "salon_service"],
                name="specialistservice_specialist_salon_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="specsvc_tenant_active_idx",
            ),
            models.Index(
                fields=["specialist", "is_active"],
                name="specsvc_spec_active_idx",
            ),
            models.Index(
                fields=["salon_service", "is_active"],
                name="specsvc_salon_active_idx",
            ),
        ]

    def resolved_duration(self) -> int | None:
        """First non-null of specialist -> salon -> template duration."""
        if self.duration_minutes is not None:
            return self.duration_minutes
        salon = self.salon_service
        if salon.duration_minutes is not None:
            return salon.duration_minutes
        template = salon.template
        if template is not None:
            return template.duration_default
        return None

    def resolved_requires_health_check(self) -> bool:
        """Escalate-only OR across template floor, salon, specialist (D1)."""
        salon = self.salon_service
        template = salon.template
        template_floor = (
            template.requires_health_check if template is not None else False
        )
        return bool(
            template_floor or salon.requires_health_check or self.requires_health_check
        )

    def clean(self) -> None:
        if self.is_active and self.resolved_duration() is None:
            raise ValidationError(
                {"duration_minutes": "An active bookable service needs a resolvable duration."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.tenant_id is None and self.salon_service_id is not None:
            self.tenant_id = self.salon_service.tenant_id
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.salon_service.name} — {self.specialist.display_name}"


class DraftSalonService(models.Model):
    """External-prefill staging row for onboarding "Confirm, don't create".

    An intake (YClients API-pull or CSV-bootstrap) writes a draft; a human
    confirms it -> materializes a SalonService (+ ExternalSourceMapping).
    The confirm/reject WRITE flow lands in S3C — S3A ships the model only.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    class ExternalSource(models.TextChoices):
        YCLIENTS = "yclients", "YClients"
        CSV = "csv", "CSV bootstrap"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="draft_salon_services",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    external_source = models.CharField(
        max_length=10, choices=ExternalSource.choices,
        default=ExternalSource.YCLIENTS,
    )
    external_service_id = models.CharField(max_length=64, blank=True, default="")
    suggested_template = models.ForeignKey(
        "ServiceTemplate",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    external_name = models.CharField(max_length=200)
    suggested_duration = models.PositiveIntegerField(null=True, blank=True)
    suggested_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    confirmed_salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Idempotent intake key — only when an external id is present, so
            # multiple manual/blank drafts coexist.
            models.UniqueConstraint(
                fields=["tenant", "external_source", "external_service_id"],
                condition=~models.Q(external_service_id=""),
                name="draftsalonservice_external_id_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "status"],
                name="draftsvc_tenant_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Draft<{self.status}> {self.external_name} @ {self.tenant.slug}"


class ExternalSourceMapping(models.Model):
    """Idempotent key between an external system id and an Ayla entity.

    Keyed by YClients ``service_id`` / ``staff_id`` (per tenant, since
    YClients ids are per-company). Two explicit nullable FKs rather than a
    GenericForeignKey (D6): ``external_type`` discriminates which is set.
    Guarantees a re-import re-uses the same Ayla id.
    """

    class Source(models.TextChoices):
        YCLIENTS = "yclients", "YClients"

    class ExternalType(models.TextChoices):
        SERVICE = "service", "Service"
        STAFF = "staff", "Staff"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.YCLIENTS,
    )
    external_type = models.CharField(max_length=8, choices=ExternalType.choices)
    external_id = models.CharField(max_length=64)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="external_source_mappings",
    )
    salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="external_source_mappings",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="external_source_mappings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_type", "external_id", "tenant"],
                name="externalsourcemapping_key_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "source", "external_type"],
                name="extmap_tenant_src_type_idx",
            ),
        ]

    def clean(self) -> None:
        """Exactly one target FK, matching external_type."""
        if self.external_type == self.ExternalType.SERVICE:
            if self.salon_service_id is None or self.specialist_id is not None:
                raise ValidationError(
                    "external_type 'service' requires salon_service and no specialist."
                )
        elif self.external_type == self.ExternalType.STAFF:
            if self.specialist_id is None or self.salon_service_id is not None:
                raise ValidationError(
                    "external_type 'staff' requires specialist and no salon_service."
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.source}:{self.external_type}:{self.external_id} @ {self.tenant.slug}"
