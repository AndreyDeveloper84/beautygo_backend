from django.contrib import admin

from .models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'specialist', 'price', 'duration_minutes', 'created_at')
    list_filter = ('specialist', 'created_at')
    search_fields = ('name', 'description', 'specialist__username')
    readonly_fields = ('created_at',)
