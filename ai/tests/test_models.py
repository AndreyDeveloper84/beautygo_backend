"""Tests for ai.models — Conversation and Message."""
from __future__ import annotations

import uuid

import pytest

from ai.models import Conversation, Message
from ai.tests.factories import make_conversation, make_message, make_user


@pytest.mark.django_db
class TestConversationModel:
    def test_uuid_pk(self):
        user = make_user()
        conv = make_conversation(user=user)
        assert isinstance(conv.id, uuid.UUID)

    def test_defaults(self):
        user = make_user()
        conv = make_conversation(user=user)
        assert conv.is_active is True
        assert conv.tenant_id is None
        assert conv.deleted_at is None
        assert conv.last_message_at is None

    def test_tenant_id_stored_and_retrieved(self):
        user = make_user()
        tid = uuid.uuid4()
        conv = make_conversation(user=user, tenant_id=tid)
        conv.refresh_from_db()
        assert conv.tenant_id == tid

    def test_tenant_id_nullable(self):
        user = make_user()
        conv = make_conversation(user=user, tenant_id=None)
        conv.refresh_from_db()
        assert conv.tenant_id is None

    def test_filter_by_tenant_id(self):
        tid = uuid.uuid4()
        user = make_user()
        make_conversation(user=user, tenant_id=tid)
        make_conversation(user=user, tenant_id=tid)
        make_conversation(user=user, tenant_id=uuid.uuid4())
        make_conversation(user=user, tenant_id=None)
        assert Conversation.objects.filter(tenant_id=tid).count() == 2

    def test_multiple_conversations_per_user(self):
        user = make_user()
        for _ in range(3):
            make_conversation(user=user)
        assert Conversation.objects.filter(user=user).count() == 3

    def test_cascade_delete_removes_messages(self):
        user = make_user()
        conv = make_conversation(user=user)
        make_message(conversation=conv, role=Message.Role.USER)
        make_message(conversation=conv, role=Message.Role.ASSISTANT)
        conv_id = conv.id
        conv.delete()
        assert Message.objects.filter(conversation_id=conv_id).count() == 0

    def test_str(self):
        user = make_user()
        conv = make_conversation(user=user)
        assert str(conv.id) in str(conv)

    def test_composite_index_exists(self):
        index_names = [idx.name for idx in Conversation._meta.indexes]
        assert any("tenant" in name for name in index_names)


@pytest.mark.django_db
class TestMessageModel:
    def test_uuid_pk(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv)
        assert isinstance(msg.id, uuid.UUID)

    def test_role_choices_all_valid(self):
        user = make_user()
        conv = make_conversation(user=user)
        for role in (
            Message.Role.USER,
            Message.Role.ASSISTANT,
            Message.Role.TOOL,
            Message.Role.SYSTEM,
        ):
            msg = make_message(conversation=conv, role=role, content=f"msg {role}")
            assert msg.role == role

    def test_action_type_raw_string(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv, action_type="future_action_v9")
        msg.refresh_from_db()
        assert msg.action_type == "future_action_v9"

    def test_action_type_defaults_empty(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv)
        assert msg.action_type == ""

    def test_action_data_json(self):
        user = make_user()
        conv = make_conversation(user=user)
        data = {"specialist_id": str(uuid.uuid4()), "slot": "10:00"}
        msg = make_message(conversation=conv, action_data=data)
        msg.refresh_from_db()
        assert msg.action_data == data

    def test_tool_call_fields_default(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv)
        assert msg.tool_call is None
        assert msg.tool_call_id == ""

    def test_tokens_default_zero(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv)
        assert msg.tokens_in == 0
        assert msg.tokens_out == 0

    def test_str(self):
        user = make_user()
        conv = make_conversation(user=user)
        msg = make_message(conversation=conv, content="Привет, мир!")
        assert "user" in str(msg)
        assert "Привет" in str(msg)
