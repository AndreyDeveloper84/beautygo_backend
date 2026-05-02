"""Admin registration for Tenant (DRF-242.1).

Uses Unfold-styled admin to match the rest of the project. Read-only on
``id`` and the timestamps; everything else is editable so a maintainer can
rename / deactivate a tenant from the admin without a migration.
"""
from __future__ import annotations

from django.contrib import admin

from tenants.models import Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("id", "slug", "name", "is_active")}),
        ("Системное", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        # Admin must see deactivated tenants too — use all_objects manager.
        return Tenant.all_objects.all()
