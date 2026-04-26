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


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    is_active = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "AI Conversation"
        verbose_name_plural = "AI Conversations"
        ordering = ["-last_message_at", "-created_at"]
        indexes = [
            models.Index(fields=["user", "-last_message_at"]),
            models.Index(fields=["is_active", "-last_message_at"]),
        ]

    def __str__(self) -> str:
        return f"Conversation {self.id} (user={self.user_id})"


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
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:50]}"
