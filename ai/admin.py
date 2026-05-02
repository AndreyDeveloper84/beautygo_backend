"""Read-only Django Admin for AI Conversation + Message.

We never edit AI history through the admin — these are immutable
audit records. Allowing add/delete would risk corrupting an audit
trail used to investigate billing disputes or LLM-driven booking
issues.
"""
from __future__ import annotations

from django.contrib import admin

from ai.models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("role", "content", "action_type", "tokens_in", "tokens_out", "created_at")
    readonly_fields = fields
    ordering = ("created_at",)


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "tenant", "is_active", "deleted_at",
        "last_message_at", "created_at",
    )
    list_filter = ("is_active", "tenant")
    search_fields = ("id", "user__username", "user__phone", "tenant__slug")
    readonly_fields = (
        "id", "user", "tenant", "is_active", "deleted_at",
        "last_message_at", "created_at",
    )
    raw_id_fields = ("tenant",)
    inlines = [MessageInline]

    def get_queryset(self, request):
        # Admin needs visibility into soft-deleted rows for audit /
        # billing-dispute investigation. Default manager hides them.
        return Conversation.all_objects.all()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "id", "conversation", "role", "action_type",
        "tokens_in", "tokens_out", "latency_ms", "created_at",
    )
    list_filter = ("role", "action_type")
    search_fields = ("id", "conversation__id", "content")
    readonly_fields = (
        "id", "conversation", "role", "content",
        "action_type", "action_data",
        "tool_call", "tool_call_id",
        "tokens_in", "tokens_out", "latency_ms",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
