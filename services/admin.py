from django.contrib import admin

from .models import Service, ServiceCategory


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'slug', 'icon', 'sort_order', 'is_active')
    list_filter = ('is_active', 'parent')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'specialist', 'category', 'price',
        'duration_minutes', 'is_active', 'sort_order', 'created_at',
    )
    list_filter = ('category', 'is_active', 'specialist', 'created_at')
    search_fields = ('name', 'description', 'specialist__username')
    readonly_fields = ('created_at',)
