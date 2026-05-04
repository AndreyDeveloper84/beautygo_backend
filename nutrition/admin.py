"""Django admin for nutrition models — content management for the team.

Beverage admin (DRF-301) lets the content owner add/edit catalog rows,
tweak aliases and water_coefficient values, and toggle is_active without
a redeploy. The seed_beverages command will *overwrite* admin edits on
re-run, so document that flow with the team (or use --only-new).
"""
from __future__ import annotations

from django.contrib import admin

from nutrition.models import Beverage, WaterEntry


@admin.register(Beverage)
class BeverageAdmin(admin.ModelAdmin):
    list_display = (
        "name_ru",
        "slug",
        "category",
        "water_coefficient",
        "kcal_per_100ml",
        "caffeine_mg_per_100ml",
        "is_active",
        "updated_at",
    )
    list_filter = ("category", "is_active")
    search_fields = ("slug", "name_ru", "aliases")
    list_editable = ("water_coefficient", "is_active")
    ordering = ("category", "name_ru")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (None, {
            "fields": ("slug", "name_ru", "category", "is_active"),
        }),
        ("Гидратация", {
            "fields": ("water_coefficient",),
            "description": "1.0 = вода. <1.0 — слабый диуретик. "
                           "Отрицательно — крепкий алкоголь.",
        }),
        ("Макро на 100 мл", {
            "fields": (
                "kcal_per_100ml",
                "protein_g_per_100ml",
                "fat_g_per_100ml",
                "carbs_g_per_100ml",
                "sugar_g_per_100ml",
                "caffeine_mg_per_100ml",
            ),
        }),
        ("Парсинг и UI", {
            "fields": ("aliases", "default_serving_ml", "default_serving_label"),
        }),
        ("Метаданные", {
            "fields": ("created_at", "updated_at"),
        }),
    )


@admin.register(WaterEntry)
class WaterEntryAdmin(admin.ModelAdmin):
    list_display = (
        "ts",
        "user",
        "ml",
        "water_ml",
        "beverage",
        "kcal",
        "milestone_threshold",
        "deleted_at",
    )
    list_filter = ("deleted_reason", "milestone_threshold")
    search_fields = ("user__username", "beverage__slug")
    readonly_fields = ("created_at",)
    raw_id_fields = ("user", "tenant", "beverage", "food_log")
    date_hierarchy = "ts"
