"""Django Admin configuration for services app."""
from __future__ import annotations

from django.contrib import admin

from .models import (
    DraftSalonService,
    ExternalSourceMapping,
    RegionalPricing,
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 0
    fields = ('name', 'price', 'duration_minutes', 'is_active')
    readonly_fields = ('created_at',)
    show_change_link = True


class ServiceTemplateInline(admin.TabularInline):
    model = ServiceTemplate
    extra = 0
    fields = (
        'name', 'name_short', 'duration_default',
        'duration_min', 'duration_max', 'is_popular', 'sort_order',
    )
    show_change_link = True


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'slug', 'icon', 'sort_order', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('sort_order', 'name')
    inlines = [ServiceTemplateInline]


class RegionalPricingInline(admin.TabularInline):
    model = RegionalPricing
    extra = 0
    fields = ('region_key', 'region_name', 'price_min', 'price_max')
    ordering = ('region_key',)


@admin.register(ServiceTemplate)
class ServiceTemplateAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'category', 'duration_default',
        'is_popular', 'sort_order',
    )
    list_filter = ('category', 'is_popular')
    search_fields = ('name', 'name_short', 'category__name')
    list_editable = ('is_popular', 'sort_order')
    ordering = ('category', '-is_popular', 'sort_order', 'name')
    inlines = [RegionalPricingInline]


@admin.register(RegionalPricing)
class RegionalPricingAdmin(admin.ModelAdmin):
    list_display = (
        'template', 'region_key', 'region_name',
        'price_min', 'price_max',
    )
    list_filter = ('region_key', 'template__category')
    search_fields = ('template__name', 'region_key', 'region_name')
    ordering = ('template__category', 'template', 'region_key')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'get_specialist_name', 'category',
        'price', 'duration_minutes', 'is_active', 'created_at',
    )
    list_filter = ('category', 'is_active', 'created_at')
    search_fields = ('name', 'description', 'specialist__display_name', 'specialist__user__phone')
    readonly_fields = ('created_at',)
    raw_id_fields = ('specialist',)
    ordering = ('specialist', 'sort_order', 'name')

    @admin.display(description='Мастер')
    def get_specialist_name(self, obj: Service) -> str:
        return obj.specialist.display_name


# --- S3A canonical catalog rebuild (#1044 / #200) ---


class SpecialistServiceInline(admin.TabularInline):
    model = SpecialistService
    extra = 0
    # D2 nuance: requires_health_check editable so ops can set custom /
    # salon-specific health-check on off-taxonomy services.
    fields = (
        'specialist', 'duration_minutes', 'price',
        'requires_health_check', 'buffer_after_minutes', 'is_active',
    )
    raw_id_fields = ('specialist',)
    show_change_link = True


@admin.register(SalonService)
class SalonServiceAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'tenant', 'template', 'category',
        'duration_minutes', 'requires_health_check', 'is_active', 'source',
    )
    list_filter = ('is_active', 'source', 'requires_health_check', 'tenant')
    search_fields = ('name', 'tenant__slug', 'template__name')
    raw_id_fields = ('template', 'category')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'name')
    inlines = [SpecialistServiceInline]


@admin.register(SpecialistService)
class SpecialistServiceAdmin(admin.ModelAdmin):
    list_display = (
        'salon_service', 'specialist', 'tenant',
        'duration_minutes', 'price', 'requires_health_check', 'is_active',
    )
    list_filter = ('is_active', 'requires_health_check', 'tenant')
    search_fields = ('salon_service__name', 'specialist__display_name')
    raw_id_fields = ('salon_service', 'specialist')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'salon_service')


@admin.register(DraftSalonService)
class DraftSalonServiceAdmin(admin.ModelAdmin):
    list_display = (
        'external_name', 'tenant', 'status', 'external_source',
        'external_service_id', 'suggested_template', 'created_at',
    )
    list_filter = ('status', 'external_source', 'tenant')
    search_fields = ('external_name', 'external_service_id', 'tenant__slug')
    raw_id_fields = ('suggested_template', 'confirmed_salon_service', 'confirmed_by')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'status', 'external_name')


@admin.register(ExternalSourceMapping)
class ExternalSourceMappingAdmin(admin.ModelAdmin):
    list_display = (
        'source', 'external_type', 'external_id', 'tenant',
        'salon_service', 'specialist',
    )
    list_filter = ('source', 'external_type', 'tenant')
    search_fields = ('external_id', 'tenant__slug')
    raw_id_fields = ('salon_service', 'specialist')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('tenant', 'external_type', 'external_id')
