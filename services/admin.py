"""Django Admin configuration for services app."""
from __future__ import annotations

from django.contrib import admin

from .models import Service, ServiceCategory


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 0
    fields = ('name', 'price', 'duration_minutes', 'is_active')
    readonly_fields = ('created_at',)
    show_change_link = True


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'slug', 'icon', 'sort_order', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('sort_order', 'name')


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
