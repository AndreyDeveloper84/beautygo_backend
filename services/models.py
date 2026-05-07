from __future__ import annotations

import uuid
from typing import Any

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
    duration_default = models.PositiveIntegerField(
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_min = models.PositiveIntegerField(
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_max = models.PositiveIntegerField(
        validators=[MinValueValidator(5), MaxValueValidator(480)],
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
        if self.duration_min > self.duration_default:
            raise ValidationError(
                {'duration_min': 'duration_min must be <= duration_default.'}
            )
        if self.duration_max < self.duration_default:
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
