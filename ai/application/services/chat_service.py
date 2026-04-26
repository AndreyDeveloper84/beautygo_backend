"""ChatService — main pipeline for POST /api/v1/ai/chat/.

Flow (per docs/AI_CHAT_PLAN.md rev. 2):

  1. resolve_conversation()              — get/create per actor
  2. check_anonymous_limit()              — 5 user-msgs cap on guests
  3. check_daily_token_limit()            — Redis daily token counter
  4. save_user_message()                  — raw content stored
  5. build_llm_context()                  — system prompt + recent 10 msgs
  6. call_llm()                           — OpenAI chat.completions
  7. parse_response() + dispatch_tool()   — turn tool_call into action_data
  8. save_assistant_message()             — with action attached
  9. update_counters()                    — last_message_at + Redis tokens
 10. return ChatResponseDTO

OpenAI errors do NOT break the chat — they raise AIUnavailable which the
view turns into 503. Throttle/limit failures raise specific exceptions
the view maps to 429 with details.reason.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.cache import cache as default_cache
from django.utils import timezone

from ai.application.services.specialist_context_builder import (
    SpecialistContext,
    SpecialistContextBuilder,
)
from ai.exceptions import (
    AIAnonymousLimitExceeded,
    AIDailyLimitExceeded,
    AIUnavailable,
)
from ai.models import Conversation, Message
from ai.prompts import render_system_prompt
from ai.redaction import redact_pii
from ai.services.llm_client import get_openai_client
from ai.tools import TOOL_DEFINITIONS, ActionType
from ai.tools_handlers import dispatch_tool_call

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
    """Orchestrates a single chat turn."""

    def __init__(
        self,
        *,
        context_builder: SpecialistContextBuilder | None = None,
        cache=None,
    ) -> None:
        self._context_builder = context_builder or SpecialistContextBuilder()
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
        conversation = self._resolve_conversation(actor, conversation_id)

        if getattr(actor, "is_guest", False):
            self._check_anonymous_limit(conversation)

        self._check_daily_token_limit(actor)

        user_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content=message_text,
        )

        spec_context = self._build_specialist_context(actor, request_context)
        history = self._load_recent_history(conversation, exclude_id=user_message.id)
        system_prompt = self._build_system_prompt(actor, request_context, spec_context)

        llm_messages = self._compose_llm_messages(
            system_prompt, history, message_text
        )

        started = time.monotonic()
        completion = self._call_openai(llm_messages)
        latency_ms = int((time.monotonic() - started) * 1000)

        content, tool_call_raw, action_type, action_data = self._parse_completion(
            completion, spec_context, actor
        )

        usage = getattr(completion, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0

        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=content or "",
            action_type=action_type or "",
            action_data=action_data,
            tool_call=tool_call_raw,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )

        self._update_counters(
            conversation=conversation,
            actor=actor,
            tokens_total=tokens_in + tokens_out,
        )

        return ChatResponseDTO(
            conversation_id=conversation.id,
            message=assistant_message,
            action_type=action_type or "",
            action_data=action_data,
        )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------
    def _resolve_conversation(
        self, actor, conversation_id: UUID | None
    ) -> Conversation:
        if conversation_id is not None:
            conv = Conversation.objects.filter(
                id=conversation_id, user=actor, is_active=True
            ).first()
            if conv is not None:
                return conv
            # Unknown / not-owned id → silently start a fresh one. The
            # alternative (404) is harsher than needed for chat UX.
        return Conversation.objects.create(user=actor, is_active=True)

    def _check_anonymous_limit(self, conversation: Conversation) -> None:
        cap = settings.AI_ANON_MESSAGE_CAP
        used = Message.objects.filter(
            conversation__user=conversation.user,
            role=Message.Role.USER,
        ).count()
        # `used` already includes the message we just persisted? No — we
        # check BEFORE saving. So `used >= cap` means this would be the
        # (cap+1)-th and must be blocked.
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

    def _build_specialist_context(
        self, actor, ctx: ChatRequestContext
    ) -> SpecialistContext:
        # Override profile location with request context if provided.
        lat = ctx.location_lat
        lon = ctx.location_lon
        if (lat is None or lon is None) and hasattr(actor, "profile"):
            profile = getattr(actor, "profile", None)
            if profile is not None:
                lat = lat or (
                    float(profile.default_location_lat)
                    if profile.default_location_lat is not None
                    else None
                )
                lon = lon or (
                    float(profile.default_location_lng)
                    if profile.default_location_lng is not None
                    else None
                )
        return self._context_builder.build(client_lat=lat, client_lon=lon)

    def _load_recent_history(
        self, conversation: Conversation, *, exclude_id: UUID | None
    ) -> list[Message]:
        qs = Message.objects.filter(conversation=conversation)
        if exclude_id is not None:
            qs = qs.exclude(id=exclude_id)
        # Take last N, then reverse to chronological for the LLM.
        recent = list(
            qs.order_by("-created_at")[: settings.AI_HISTORY_WINDOW]
        )
        recent.reverse()
        return recent

    def _build_system_prompt(
        self, actor, ctx: ChatRequestContext, spec_context: SpecialistContext
    ) -> str:
        profile = getattr(actor, "profile", None)
        city = getattr(profile, "city", None) if profile else None
        first_name = getattr(actor, "first_name", "") or None
        location_hint = None
        if ctx.location_lat is not None and ctx.location_lon is not None:
            location_hint = f"{ctx.location_lat:.4f},{ctx.location_lon:.4f}"
        return render_system_prompt(
            city=city,
            first_name=first_name,
            today=date.today(),
            location_hint=location_hint,
            specialists_summary=spec_context.to_prompt_summary(),
        )

    def _compose_llm_messages(
        self,
        system_prompt: str,
        history: list[Message],
        user_message_text: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        for m in history:
            if m.role == Message.Role.USER:
                messages.append(
                    {"role": "user", "content": redact_pii(m.content)}
                )
            elif m.role == Message.Role.ASSISTANT:
                messages.append(
                    {"role": "assistant", "content": m.content}
                )
            # tool / system messages skipped — the LLM doesn't need them
            # for short-window context.
        messages.append(
            {"role": "user", "content": redact_pii(user_message_text)}
        )
        return messages

    def _call_openai(self, messages: list[dict[str, Any]]):
        try:
            client = get_openai_client()
            return client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                max_tokens=600,
                temperature=0.6,
            )
        except Exception as exc:  # noqa: BLE001 — vendor failure → 503
            logger.exception("ai.openai.error: %s", exc)
            raise AIUnavailable(str(exc)) from exc

    def _parse_completion(
        self,
        completion,
        spec_context: SpecialistContext,
        actor,
    ):
        """Return (content, tool_call_raw, action_type, action_data)."""
        choice = completion.choices[0]
        msg = choice.message
        content = getattr(msg, "content", "") or ""

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return content, None, "", None

        # MVP: handle only the first tool call. Multi-tool responses
        # would need a tool-result roundtrip we don't do in sync mode.
        tc = tool_calls[0]
        fn = getattr(tc, "function", None)
        if fn is None:
            return content, None, "", None

        try:
            import json
            args = json.loads(fn.arguments or "{}")
        except (ValueError, TypeError):
            args = {}

        client_id = actor.id if not getattr(actor, "is_guest", False) else None
        result = dispatch_tool_call(
            fn.name,
            args,
            context=spec_context,
            client_id=client_id,
        )
        tool_call_raw = {
            "id": getattr(tc, "id", ""),
            "name": fn.name,
            "arguments": fn.arguments,
        }
        return content, tool_call_raw, result.action_type, result.action_data

    def _update_counters(
        self,
        *,
        conversation: Conversation,
        actor,
        tokens_total: int,
    ) -> None:
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message_at"])
        if tokens_total > 0:
            key = self._daily_tokens_key(actor.id)
            try:
                self._cache.incr(key, tokens_total)
            except ValueError:
                # Key didn't exist — set with 24h TTL.
                self._cache.set(key, tokens_total, timeout=86400)

    @staticmethod
    def _daily_tokens_key(user_id: UUID) -> str:
        today = datetime.now(dt_timezone.utc).date().isoformat()
        return f"ai:tokens:{user_id}:{today}"
