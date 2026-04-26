"""Unit tests for ChatService."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ai.application.services.chat_service import (
    ChatRequestContext,
    ChatService,
)
from ai.exceptions import (
    AIAnonymousLimitExceeded,
    AIDailyLimitExceeded,
    AIUnavailable,
)
from ai.models import Conversation, Message
from ai.tests.factories import make_conversation, make_message, make_specialist
from ai.tools import ActionType


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Conversation resolution
# ---------------------------------------------------------------------------


class TestConversationResolution:
    def test_creates_new_conversation_when_id_missing(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion(
            content="Hi"
        )
        result = ChatService().send_message(
            actor=client_user,
            conversation_id=None,
            message_text="Hi",
        )
        assert result.conversation_id is not None
        assert Conversation.objects.filter(id=result.conversation_id).exists()

    def test_uses_existing_conversation_when_owned(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        existing = make_conversation(user=client_user)
        patch_openai.chat.completions.create.return_value = fake_completion()
        result = ChatService().send_message(
            actor=client_user,
            conversation_id=existing.id,
            message_text="Hi",
        )
        assert result.conversation_id == existing.id

    def test_unknown_id_starts_fresh_conversation(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        import uuid as _uuid

        patch_openai.chat.completions.create.return_value = fake_completion()
        bogus_id = _uuid.uuid4()
        result = ChatService().send_message(
            actor=client_user,
            conversation_id=bogus_id,
            message_text="Hi",
        )
        assert result.conversation_id != bogus_id


# ---------------------------------------------------------------------------
# Anonymous + daily limits
# ---------------------------------------------------------------------------


class TestLimits:
    def test_guest_under_cap_works(
        self, patch_openai, fake_completion, guest_user, specialist_a, settings
    ):
        settings.AI_ANON_MESSAGE_CAP = 5
        patch_openai.chat.completions.create.return_value = fake_completion()
        ChatService().send_message(
            actor=guest_user, conversation_id=None, message_text="привет"
        )

    def test_guest_at_cap_raises(
        self, patch_openai, fake_completion, guest_user, specialist_a, settings
    ):
        settings.AI_ANON_MESSAGE_CAP = 2
        conv = make_conversation(user=guest_user)
        make_message(conversation=conv, role=Message.Role.USER)
        make_message(conversation=conv, role=Message.Role.USER)
        patch_openai.chat.completions.create.return_value = fake_completion()
        with pytest.raises(AIAnonymousLimitExceeded):
            ChatService().send_message(
                actor=guest_user, conversation_id=conv.id, message_text="ещё"
            )

    def test_daily_token_cap_raises(
        self, patch_openai, fake_completion, client_user, specialist_a, settings
    ):
        from django.core.cache import cache

        settings.AI_MAX_TOKENS_PER_USER_PER_DAY = 100
        cache.set(
            ChatService._daily_tokens_key(client_user.id), 200, timeout=60
        )
        patch_openai.chat.completions.create.return_value = fake_completion()
        with pytest.raises(AIDailyLimitExceeded):
            ChatService().send_message(
                actor=client_user, conversation_id=None, message_text="x"
            )


# ---------------------------------------------------------------------------
# 503 when API key missing
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_empty_key_raises_unavailable(self, settings, client_user):
        settings.OPENAI_API_KEY = ""
        with pytest.raises(AIUnavailable):
            ChatService().send_message(
                actor=client_user, conversation_id=None, message_text="x"
            )

    def test_openai_exception_becomes_unavailable(
        self, patch_openai, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.side_effect = RuntimeError("boom")
        with pytest.raises(AIUnavailable):
            ChatService().send_message(
                actor=client_user, conversation_id=None, message_text="x"
            )


# ---------------------------------------------------------------------------
# PII redaction before OpenAI
# ---------------------------------------------------------------------------


class TestPIIRedaction:
    def test_phone_redacted_in_outgoing_messages_but_raw_in_db(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion()
        raw = "Позвони +79991234567 завтра"
        ChatService().send_message(
            actor=client_user, conversation_id=None, message_text=raw
        )

        # Inspect what was sent to OpenAI.
        sent_messages = patch_openai.chat.completions.create.call_args.kwargs[
            "messages"
        ]
        last = sent_messages[-1]
        assert last["role"] == "user"
        assert "+79991234567" not in last["content"]
        assert "[PHONE]" in last["content"]

        # Raw stays in DB.
        stored = Message.objects.filter(
            role=Message.Role.USER, content__icontains="79991234567"
        ).first()
        assert stored is not None


# ---------------------------------------------------------------------------
# Tool call dispatching
# ---------------------------------------------------------------------------


class TestToolCalls:
    def test_no_tool_call_returns_text_message(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion(
            content="Уточните что вы хотели?"
        )
        result = ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="да"
        )
        assert result.action_type == ""
        assert result.action_data is None
        assert "Уточните" in result.message.content

    def test_show_specialists_tool_attaches_action_data(
        self,
        patch_openai,
        fake_completion,
        fake_tool_call,
        client_user,
        specialist_a,
        specialist_b,
    ):
        tc = fake_tool_call(
            "show_specialists",
            {
                "specialist_ids": [str(specialist_a.id), str(specialist_b.id)],
                "match_scores": [95, 80],
                "match_reasons": [["Высокий рейтинг"], ["Близко"]],
                "explanation": "Топ под запрос",
            },
        )
        patch_openai.chat.completions.create.return_value = fake_completion(
            content="Вот мастера:", tool_calls=[tc]
        )
        result = ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="маникюр"
        )
        assert result.action_type == ActionType.SHOW_SPECIALISTS
        assert len(result.action_data["specialists"]) == 2
        assert result.action_data["explanation"] == "Топ под запрос"

    def test_invalid_specialist_id_falls_back_to_clarification(
        self,
        patch_openai,
        fake_completion,
        fake_tool_call,
        client_user,
        specialist_a,
    ):
        import uuid as _uuid

        tc = fake_tool_call(
            "show_specialists",
            {
                "specialist_ids": [str(_uuid.uuid4())],
                "explanation": "fake",
            },
        )
        patch_openai.chat.completions.create.return_value = fake_completion(
            tool_calls=[tc]
        )
        result = ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="маникюр"
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION

    def test_show_appointments_blocked_for_guest(
        self,
        patch_openai,
        fake_completion,
        fake_tool_call,
        guest_user,
    ):
        tc = fake_tool_call(
            "show_appointments", {"filter": "upcoming"}
        )
        patch_openai.chat.completions.create.return_value = fake_completion(
            tool_calls=[tc]
        )
        result = ChatService().send_message(
            actor=guest_user, conversation_id=None, message_text="мои записи?"
        )
        # Guest hits anonymous-blocked path → fallback clarification.
        assert result.action_type == ActionType.ASK_CLARIFICATION


# ---------------------------------------------------------------------------
# Persistence + counters
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_persists_user_then_assistant(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion(
            content="ok", tokens_in=15, tokens_out=25
        )
        result = ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="hi"
        )
        msgs = list(
            Message.objects.filter(
                conversation_id=result.conversation_id
            ).order_by("created_at")
        )
        assert [m.role for m in msgs] == [
            Message.Role.USER, Message.Role.ASSISTANT
        ]
        assistant = msgs[1]
        assert assistant.tokens_in == 15
        assert assistant.tokens_out == 25
        assert assistant.latency_ms is not None

    def test_updates_last_message_at(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion()
        result = ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="hi"
        )
        conv = Conversation.objects.get(id=result.conversation_id)
        assert conv.last_message_at is not None

    def test_increments_daily_token_counter(
        self, patch_openai, fake_completion, client_user, specialist_a
    ):
        from django.core.cache import cache

        patch_openai.chat.completions.create.return_value = fake_completion(
            tokens_in=10, tokens_out=20
        )
        ChatService().send_message(
            actor=client_user, conversation_id=None, message_text="hi"
        )
        key = ChatService._daily_tokens_key(client_user.id)
        assert cache.get(key) == 30
