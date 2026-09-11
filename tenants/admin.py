"""Admin registration for Tenant (DRF-242.1).

Uses Unfold-styled admin to match the rest of the project. Read-only on
``id`` and the timestamps; everything else is editable so a maintainer can
rename / deactivate a tenant from the admin without a migration.
"""
from __future__ import annotations

from django.contrib import admin

from tenants.models import ServiceLocation, Tenant
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


class ServiceLocationInline(admin.TabularInline):
    """Места салона — рядом с ним, чтобы подтверждающий видел, что уже есть."""

    model = ServiceLocation
    extra = 0
    fields = ("label", "address", "city", "status", "geocode_status", "latitude", "longitude")
    readonly_fields = ("geocode_status", "latitude", "longitude")
    show_change_link = True


TenantAdmin.inlines = TenantAdmin.inlines + (ServiceLocationInline,)


@admin.register(ServiceLocation)
class ServiceLocationAdmin(admin.ModelAdmin):
    """Очередь подтверждения мест (§9, DRF-1687).

    Фильтр по ``status`` — рабочий инструмент: «покажи всё, что требует
    проверки». Подтверждение без автора, времени и основания форма не
    пропустит (``clean()`` модели), а ``update()`` мимо формы — не пропустит
    схема (``servicelocation_confirmed_requires_provenance``).

    Координаты и восемь полей происхождения — только на чтение: их пишет
    геокодер (``core.geocoding``), не человек. Человек подтверждает МЕСТО;
    подтверждение КООРДИНАТ неоднозначного адреса — отдельная поверхность,
    которой пока нет ни у кого (см. DRF-1349).
    """

    list_display = (
        "__str__", "tenant", "city", "status", "geocode_status",
        "confirmed_by", "confirmed_at", "updated_at",
    )
    list_filter = ("status", "geocode_status", "tenant", "city")
    search_fields = ("label", "address", "city", "tenant__slug", "tenant__name",
                     "geocode_normalized_address")
    raw_id_fields = ("tenant", "confirmed_by")
    readonly_fields = (
        "id", "geocode_source_address", "geocode_normalized_address",
        "latitude", "longitude", "geocode_provider", "geocode_precision",
        "geocode_status", "geocoded_at", "created_at", "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("id", "tenant", "label", "address", "city")}),
        ("Подтверждение места (§9)", {
            "fields": ("status", "confirmed_by", "confirmed_at", "confirmed_source_ref"),
            "description": (
                "confirmed требует всех трёх полей: кто, когда, на каком основании. "
                "review_required — происхождение неизвестно. inactive — тестовый, "
                "личный или недействительный адрес."
            ),
        }),
        ("Координаты и происхождение (§139) — пишет геокодер", {
            "fields": (
                "geocode_status", "latitude", "longitude", "geocode_precision",
                "geocode_provider", "geocode_source_address",
                "geocode_normalized_address", "geocoded_at",
            ),
            "classes": ("collapse",),
        }),
        ("Системное", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("tenant", "confirmed_by")
