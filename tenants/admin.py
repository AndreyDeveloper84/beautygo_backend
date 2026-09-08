"""Admin registration for Tenant (DRF-242.1).

Uses Unfold-styled admin to match the rest of the project. Read-only on
``id`` and the timestamps; everything else is editable so a maintainer can
rename / deactivate a tenant from the admin without a migration.
"""
from __future__ import annotations

from django.contrib import admin

from tenants.models import Tenant
from users.admin import TenantMastersInline


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    # DRF-1596 — до этого тикета кортеж был пуст, и владелец, открыв
    # `/admin/tenants/tenant/add/`, чтобы завести настоящий салон с
    # мастерами, добавить мастера не мог: форма салона про мастеров не
    # знала вовсе. Блок определён в `users.admin` (рядом с моделью и с
    # `SpecialistProfileAdmin`), чтобы подсказки и набор полей у мастера
    # были одни и те же, где бы его ни заводили.
    inlines = (TenantMastersInline,)

    list_display = ("name", "slug", "city", "is_active", "created_at")
    list_filter = ("is_active", "city")
    search_fields = ("name", "slug", "city", "address")
    readonly_fields = ("id", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("id", "slug", "name", "is_active")}),
        # DRF-1587 — единственное место, где адрес и город салона можно
        # завести в Ayla. До этого тикета их не было в источнике вовсе:
        # город существовал только в зеркале бота, куда его вписывал
        # оператор (`create_tenant --city`), а адрес — на профиле каждого
        # мастера отдельной копией одной и той же строки.
        ("Адрес", {
            "fields": ("city", "address"),
            "description": (
                "Пустое поле означает «не указано» и уезжает наружу как "
                "null. Салон без города не попадает ни в один городской "
                "ответ поиска."
            ),
        }),
        ("Системное", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        # Admin must see deactivated tenants too — use all_objects manager.
        return Tenant.all_objects.all()
