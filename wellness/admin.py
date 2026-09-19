"""Админка wellness — шаблоны Plan Lite (DRF-2123).

Только ``PlanTemplate``: это курируемые данные владельца («цель → 1–3
обязательства + почему»), и править их нужно без релиза. Планы, действия
и наблюдения людей в админку не выводятся — это персональные данные под
своими гейтами.
"""
from __future__ import annotations

from django.contrib import admin

from .models import PlanTemplate


@admin.register(PlanTemplate)
class PlanTemplateAdmin(admin.ModelAdmin):
    list_display = ("goal_key", "version", "is_active", "created_at")
    list_filter = ("is_active", "goal_key")
    search_fields = ("goal_key", "why_text")
    ordering = ("goal_key", "-version")
    readonly_fields = ("created_at",)
    fields = ("goal_key", "version", "is_active", "actions", "why_text", "created_at")
