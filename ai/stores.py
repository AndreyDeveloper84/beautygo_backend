"""DjangoConversationStore — Django ORM adapter for ayla-ai-core's ConversationStore.

`AIConcierge` from ayla-ai-core (DRF-237) is persistence-agnostic: it expects
any object implementing the `ConversationStore` Protocol — three sync methods
that the orchestrator wraps in `sync_to_async`. This module provides Ayla's
concrete implementation against the `ai.models.Conversation` / `Message`
tables (DRF-240).

Why sync (not async): the protocol commits to sync — AIConcierge handles the
sync→async bridge once, callers don't repeat themselves. Doing async ORM
here would also break the partial-unique fast-path (`select_for_update`
isn't supported on async cursors yet).

Tenant scoping: `user_key` is the request's `User` instance. `user.tenant`
(DRF-242.3 FK) provides the tenant binding — store reads it directly so the
`(user, tenant)` partial-unique constraint on `Conversation` is honoured.
Anonymous flows and tenant=None coexist via the partial-unique condition.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from ai.models import Conversation, Message

if TYPE_CHECKING:  # pragma: no cover
    from users.models import User


__all__ = ["DjangoConversationStore"]


class DjangoConversationStore:
    """Sync ORM adapter satisfying ayla_ai_core.orchestrator.ConversationStore."""

    def resolve_active_conversation(self, user_key: "User") -> Conversation:
        """Return the user's single active conversation for their tenant.

        The Conversation model has a partial unique constraint on
        (user, tenant) WHERE is_active=true AND deleted_at IS NULL — so
        the get-or-create here is race-safe even under concurrent POST
        /ai/chat/ from the same client.
        """
        user = user_key
        tenant = getattr(user, "tenant", None)

        existing = (
            Conversation.objects
            .filter(user=user, tenant=tenant, is_active=True)
            .first()
        )
        if existing is not None:
            return existing

        # Two parallel callers can both miss the .first() check — the
        # partial-unique constraint will fire IntegrityError on the
        # losing side; we re-fetch the winner's row.
        try:
            with transaction.atomic():
                return Conversation.objects.create(
                    user=user, tenant=tenant, is_active=True,
                )
        except IntegrityError:
            return Conversation.objects.get(
                user=user, tenant=tenant, is_active=True,
            )

    def save_message(
        self,
        conversation: Conversation,
        *,
        role: str,
        content: str,
        action_type: str = "",
        action_data: dict | None = None,
        tool_call: dict | None = None,
        tool_call_id: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int | None = None,
    ) -> Message:
        """Persist Message + bump conversation.last_message_at atomically.

        `update()` not `save()` on the conversation because last_message_at
        is the only field changing — auto_now_add fields stay frozen and
        we avoid an extra UPDATE for unchanged columns.
        """
        msg = Message.objects.create(
            conversation=conversation,
            role=role,
            content=content,
            action_type=action_type or "",
            action_data=action_data,
            tool_call=tool_call,
            tool_call_id=tool_call_id or "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )
        Conversation.objects.filter(pk=conversation.pk).update(
            last_message_at=timezone.now(),
        )
        return msg

    def load_recent_history(
        self,
        conversation: Conversation,
        *,
        exclude_id: UUID | Any | None = None,
        limit: int = 10,
    ) -> list[Message]:
        """Last N messages in chronological order, optionally excluding one.

        Pattern: take last N (DESC), reverse — preserves chronology even
        when total messages > N (older ones get evicted from the LLM
        window correctly). The exclude_id hook lets the orchestrator skip
        the just-saved user message which was used as the tail of the
        prompt.
        """
        qs = Message.objects.filter(conversation=conversation)
        if exclude_id is not None:
            qs = qs.exclude(id=exclude_id)
        recent = list(qs.order_by("-created_at")[:limit])
        recent.reverse()
        return recent
