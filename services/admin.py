"""Django Admin configuration for services app."""
from __future__ import annotations

from django.contrib import admin

from .models import RegionalPricing, Service, ServiceCategory, ServiceTemplate


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
