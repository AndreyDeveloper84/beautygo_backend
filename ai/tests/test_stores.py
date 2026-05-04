"""Tests for DjangoConversationStore — DRF-241 Slice A.

Verifies the Django adapter satisfies ayla-ai-core's ConversationStore
Protocol: resolve idempotency under the partial-unique constraint, save
behaviour for the assistant-message shape AIConcierge writes, and
history loading semantics (chronological order, exclude support).
"""
from __future__ import annotations

import pytest
from ayla_ai_core.orchestrator import ConversationStore

from ai.models import Conversation
from ai.stores import DjangoConversationStore
from ai.tests.factories import make_conversation, make_message, make_user


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_implements_conversation_store_protocol(self):
        # ConversationStore is a runtime_checkable Protocol — isinstance
        # validates the three method names exist with the right shape.
        store = DjangoConversationStore()
        assert isinstance(store, ConversationStore)


# ---------------------------------------------------------------------------
# resolve_active_conversation
# ---------------------------------------------------------------------------


class TestResolveActive:
    def test_returns_existing_active_for_user_and_tenant(self):
        user = make_user()
        existing = make_conversation(user=user)
        store = DjangoConversationStore()
        resolved = store.resolve_active_conversation(user)
        assert resolved.id == existing.id

    def test_creates_new_when_none_exists(self):
        user = make_user()
        store = DjangoConversationStore()
        conv = store.resolve_active_conversation(user)
        assert conv.user_id == user.id
        assert conv.is_active is True
        # tenant stays None when User has no tenant assigned (anonymous /
        # pre-onboarding flow) — the partial-unique still works because
        # condition matches NULLs as a single bucket per user.
        assert conv.tenant is None

    def test_idempotent_under_repeat_calls(self):
        user = make_user()
        store = DjangoConversationStore()
        first = store.resolve_active_conversation(user)
        second = store.resolve_active_conversation(user)
        assert first.id == second.id
        assert Conversation.objects.filter(user=user, is_active=True).count() == 1

    def test_skips_soft_deleted(self):
        user = make_user()
        old = make_conversation(user=user)
        old.mark_deleted()
        store = DjangoConversationStore()
        new = store.resolve_active_conversation(user)
        assert new.id != old.id

    def test_reads_user_tenant_fk(self):
        from tenants.models import Tenant
        tenant = Tenant.objects.create(slug="t1", name="T1")
        user = make_user()
        user.tenant = tenant
        user.save(update_fields=["tenant"])
        store = DjangoConversationStore()
        conv = store.resolve_active_conversation(user)
        assert conv.tenant_id == tenant.id


# ---------------------------------------------------------------------------
# save_message
# ---------------------------------------------------------------------------


class TestSaveMessage:
    def test_saves_user_message_minimal_fields(self):
        user = make_user()
        conv = make_conversation(user=user)
        store = DjangoConversationStore()
        msg = store.save_message(conv, role="user", content="Привет")
        assert msg.id is not None
        assert msg.role == "user"
        assert msg.content == "Привет"
        # Defaults for assistant-only fields.
        assert msg.action_type == ""
        assert msg.action_data is None
        assert msg.tokens_in == 0

    def test_saves_assistant_message_with_action(self):
        user = make_user()
        conv = make_conversation(user=user)
        store = DjangoConversationStore()
        msg = store.save_message(
            conv,
            role="assistant",
            content="Нашла трёх мастеров",
            action_type="show_masters",
            action_data={"master_ids": ["abc"]},
            tool_call={"id": "call_1", "name": "show_masters", "arguments": "{}"},
            tool_call_id="call_1",
            tokens_in=120, tokens_out=40, latency_ms=850,
        )
        msg.refresh_from_db()
        assert msg.action_type == "show_masters"
        assert msg.action_data == {"master_ids": ["abc"]}
        assert msg.tool_call_id == "call_1"
        assert msg.tokens_in == 120
        assert msg.latency_ms == 850

    def test_bumps_conversation_last_message_at(self):
        user = make_user()
        conv = make_conversation(user=user)
        assert conv.last_message_at is None
        store = DjangoConversationStore()
        store.save_message(conv, role="user", content="x")
        conv.refresh_from_db()
        assert conv.last_message_at is not None


# ---------------------------------------------------------------------------
# load_recent_history
# ---------------------------------------------------------------------------


class TestLoadHistory:
    def test_returns_messages_in_chronological_order(self):
        user = make_user()
        conv = make_conversation(user=user)
        for i in range(3):
            make_message(conversation=conv, content=f"m{i}")
        store = DjangoConversationStore()
        recent = store.load_recent_history(conv, limit=10)
        assert [m.content for m in recent] == ["m0", "m1", "m2"]

    def test_limit_keeps_last_n_in_chronological_order(self):
        user = make_user()
        conv = make_conversation(user=user)
        for i in range(5):
            make_message(conversation=conv, content=f"m{i}")
        store = DjangoConversationStore()
        recent = store.load_recent_history(conv, limit=3)
        # Last 3 of 5, still chronological.
        assert [m.content for m in recent] == ["m2", "m3", "m4"]

    def test_exclude_id_removes_just_saved_user_msg(self):
        user = make_user()
        conv = make_conversation(user=user)
        skipme = make_message(conversation=conv, content="skip")
        keep = make_message(conversation=conv, content="keep")
        store = DjangoConversationStore()
        recent = store.load_recent_history(conv, exclude_id=skipme.id, limit=10)
        assert [m.id for m in recent] == [keep.id]

    def test_returns_empty_list_for_new_conversation(self):
        user = make_user()
        conv = make_conversation(user=user)
        store = DjangoConversationStore()
        assert store.load_recent_history(conv, limit=10) == []
