"""ChatService — main pipeline for POST /api/v1/ai/chat/.

Slice B (DRF-241): the LLM pipeline (resolve conversation → save user msg
→ build context → load history → render prompt → call OpenAI → parse +
dispatch → save assistant msg) is delegated to `ayla_ai_core.AIConcierge`
through `concierge_factory.get_concierge_for(actor)`. ChatService keeps
the Ayla-specific guardrails on top:

  1. check_anonymous_limit()              — 5 user-msgs cap on guests
  2. check_daily_token_limit()            — Redis daily token counter
  3. concierge.send_message(...)          ← ayla-ai-core does steps 4-9
  4. update_token_counter()               — Redis post-call

`tool_dispatcher` is wired to Ayla's local `ai/tools_handlers.py` so the
wire-format stays `show_specialists` / `specialist_id` per Notion API
spec v2.0 — see concierge_factory.py for the full rationale.

OpenAI errors raised by AIConcierge bubble out as AIUnavailable → 503.
Throttle/limit failures raise AIAnonymousLimitExceeded /
AIDailyLimitExceeded → 429 with `details.reason`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from typing import Any
from uuid import UUID

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.cache import cache as default_cache

from ai.concierge_factory import (
    get_concierge_for,
    render_ayla_system_prompt,
)
from ai.exceptions import (
    AIAnonymousLimitExceeded,
    AIDailyLimitExceeded,
    AIUnavailable,
)
from ai.models import Conversation, Message
from ai.redaction import redact_pii

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatRequestContext:
    location_lat: float | None = None
    location_lon: float | None = None
    preferred_date: str | None = None
    preferred_time: str | None = None
    voice_mode: bool = False  # Accepted, ignored in MVP


@dataclass(frozen=True)
class ChatResponseDTO:
    conversation_id: UUID
    message: Message
    action_type: str
    action_data: dict[str, Any] | None


class ChatService:
    """Wraps ayla-ai-core's AIConcierge with Ayla guardrails + counters."""

    def __init__(self, *, cache=None) -> None:
        self._cache = cache or default_cache

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def send_message(
        self,
        *,
        actor,  # users.models.User instance
        conversation_id: UUID | None,
        message_text: str,
        request_context: ChatRequestContext | None = None,
    ) -> ChatResponseDTO:
        if not settings.OPENAI_API_KEY:
            raise AIUnavailable("OPENAI_API_KEY is not configured")

        request_context = request_context or ChatRequestContext()

        if getattr(actor, "is_guest", False):
            self._check_anonymous_limit(actor)

        self._check_daily_token_limit(actor)

        # PII-redact before the message hits the LLM. The DB stores the
        # raw user content (AIConcierge's store.save_message in slice A
        # gets the redacted text passed in, see below — that's a
        # privacy-by-default choice: even local DB / replicas never see
        # raw phone numbers / emails).
        redacted_text = redact_pii(message_text)

        prompt_renderer = self._make_prompt_renderer(actor)
        concierge = get_concierge_for(actor)

        try:
            core_result = async_to_sync(concierge.send_message)(
                user_key=actor,
                message_text=redacted_text,
                prompt_renderer=prompt_renderer,
            )
        except (AIAnonymousLimitExceeded, AIDailyLimitExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 — vendor / pipeline failure → 503
            logger.exception("ai.concierge.error: %s", exc)
            raise AIUnavailable(str(exc)) from exc

        # AIConcierge persisted the assistant Message via DjangoConversationStore.
        # Pull it back to satisfy the (existing) ChatResponseDTO contract that
        # views.py serialises through MessageSerializer.
        assistant_message = (
            Message.objects.filter(
                conversation_id=core_result.conversation_id,
                role=Message.Role.ASSISTANT,
            )
            .order_by("-created_at")
            .first()
        )

        # Daily token counter — read off the message we just persisted.
        if assistant_message is not None:
            tokens_total = (
                (assistant_message.tokens_in or 0)
                + (assistant_message.tokens_out or 0)
            )
            if tokens_total > 0:
                self._update_token_counter(actor.id, tokens_total)

        return ChatResponseDTO(
            conversation_id=core_result.conversation_id,
            message=assistant_message,
            action_type=core_result.action_type or "",
            action_data=core_result.action_data,
        )

    # ------------------------------------------------------------------
    # Guardrails
    # ------------------------------------------------------------------
    def _check_anonymous_limit(self, actor) -> None:
        cap = settings.AI_ANON_MESSAGE_CAP
        used = Message.objects.filter(
            conversation__user=actor,
            role=Message.Role.USER,
        ).count()
        # Check BEFORE persistence — cap is the Nth allowed message. The
        # Nth+1 attempt blocks. (AIConcierge will be the one that saves
        # the new user message after this returns.)
        if used >= cap:
            raise AIAnonymousLimitExceeded(
                f"anonymous user reached {cap} message cap"
            )

    def _check_daily_token_limit(self, actor) -> None:
        key = self._daily_tokens_key(actor.id)
        used = self._cache.get(key, 0) or 0
        if used >= settings.AI_MAX_TOKENS_PER_USER_PER_DAY:
            raise AIDailyLimitExceeded(
                f"daily token cap {settings.AI_MAX_TOKENS_PER_USER_PER_DAY} reached"
            )

    def _update_token_counter(self, user_id: UUID, tokens_total: int) -> None:
        key = self._daily_tokens_key(user_id)
        try:
            self._cache.incr(key, tokens_total)
        except ValueError:
            # Key didn't exist yet — set with 24h TTL.
            self._cache.set(key, tokens_total, timeout=86400)

    # ------------------------------------------------------------------
    # Prompt rendering — closure that AIConcierge calls with the
    # ayla-ai-core core context after building it.
    # ------------------------------------------------------------------
    def _make_prompt_renderer(self, actor):
        client_name = getattr(actor, "first_name", "") or None
        today = date.today()
        # bookings_count drives tone-adjustment in the marketplace voice
        # template (new vs returning client). Guests get 0; the lookup
        # for authenticated users is cheap (indexed on client_id).
        bookings_count = self._get_bookings_count(actor)

        def _render(core_context):
            return render_ayla_system_prompt(
                core_context,
                today=today,
                client_name=client_name,
                bookings_count=bookings_count,
            )

        return _render

    @staticmethod
    def _get_bookings_count(actor) -> int:
        if getattr(actor, "is_guest", False):
            return 0
        try:
            from appointments.models import Appointment
        except ImportError:  # pragma: no cover — appointments app missing
            return 0
        return Appointment.objects.filter(client=actor).count()

    @staticmethod
    def _daily_tokens_key(user_id: UUID) -> str:
        today = datetime.now(dt_timezone.utc).date().isoformat()
        return f"ai:tokens:{user_id}:{today}"

    # ------------------------------------------------------------------
    # Pre-existing helper kept for callers that still want to look up
    # the user's active conversation outside the chat path (action view,
    # detail view). Returns None if no active conversation exists.
    # ------------------------------------------------------------------
    @staticmethod
    def get_active_conversation(actor) -> Conversation | None:
        return (
            Conversation.objects.filter(user=actor, is_active=True).first()
        )
