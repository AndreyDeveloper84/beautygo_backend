"""AI Chat persistence — Conversation + Message.

Per docs/AI_CHAT_PLAN.md rev. 2 (spec-aligned to Notion API Spec v2.0
§AI ASSISTANT). Single User FK — anonymous detected via `user.is_guest`
because every AnonymousSession already creates a User row (see
users.models.AnonymousSession.user OneToOne).

History retention: all messages persisted; LLM context truncated to
last 10 in chat_service.

Action attached to assistant message via action_type + action_data
(per spec AIMessageFull.action shape). Raw tool_call kept in
tool_call/tool_call_id for debugging.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class _ConversationManager(models.Manager):
    """Default manager — hides soft-deleted rows.

    Use `Conversation.all_objects` to include soft-deleted (admin only).
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    # tenant FK — DRF-242.2 conversion of the original tenant_id UUIDField
    # (DRF-240). The DB column stays `tenant_id` because Django's FK column
    # naming default matches the original column name, so call sites that
    # read/write `obj.tenant_id` (raw UUID) keep working — Django exposes
    # both `obj.tenant` (Tenant instance, lazy-loaded) and `obj.tenant_id`
    # (raw FK column value). PROTECT: dropping a tenant must not silently
    # delete user conversations — it's a billing/legal incident path.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversations",
    )
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = _ConversationManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "AI Conversation"
        verbose_name_plural = "AI Conversations"
        # "-id" makes the order total — see DRF-1128. last_message_at is
        # NULL until the first reply and created_at is auto_now_add.
        ordering = ["-last_message_at", "-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-last_message_at"]),
            models.Index(fields=["is_active", "-last_message_at"]),
            models.Index(fields=["tenant", "is_active", "-last_message_at"]),
        ]
        constraints = [
            # ConversationStore.resolve_active_conversation contract
            # (ayla-ai-core orchestrator) requires exactly one active
            # conversation per (user, tenant). Without this two parallel
            # POST /ai/chat/ from the same user race-condition into two
            # active rows. Postgres-only partial unique — SQLite tests
            # fall back to app-side check in resolve_active_conversation.
            models.UniqueConstraint(
                fields=["user", "tenant"],
                condition=models.Q(is_active=True, deleted_at__isnull=True),
                name="ai_conversation_one_active_per_user_tenant",
            ),
        ]

    def __str__(self) -> str:
        return f"Conversation {self.id} (user={self.user_id})"

    def mark_deleted(self) -> None:
        """Soft-delete: hide from default manager + close the conversation.

        Used by the «Очистить историю Ayla» 152-ФЗ workflow. Messages
        cascade-hide via the FK + admin-only `Conversation.all_objects`
        for audit access.
        """
        self.is_active = False
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_active", "deleted_at"])


class Message(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()

    # Action attached to this assistant message — per spec AIMessageFull.action.
    # action_type stored as raw string (not enum) so future action types don't
    # require migrations. Validation happens in serializer + tool_handlers.
    action_type = models.CharField(max_length=32, blank=True, default="")
    action_data = models.JSONField(null=True, blank=True)

    # Raw OpenAI tool_call object — kept for audit/debug. tool_call_id
    # links a tool-result message back to the originating tool_call.
    tool_call = models.JSONField(null=True, blank=True)
    tool_call_id = models.CharField(max_length=64, blank=True, default="")

    # Telemetry — populated for assistant messages.
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    latency_ms = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI Message"
        verbose_name_plural = "AI Messages"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            # Analytics workload — see BOT_CODE_AUDIT_2026-04 §1.6:
            # "SELECT action_type, COUNT(*) FROM messages WHERE
            # role='assistant' GROUP BY action_type" runs on every
            # ops dashboard. Composite (role, action_type) because
            # 99% of these queries pin role='assistant' first.
            models.Index(fields=["role", "action_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:50]}"
