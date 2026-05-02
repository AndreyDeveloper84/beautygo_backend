"""Tests for ai.models — Conversation and Message."""
from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError
from django.utils import timezone

from ai.models import Conversation, Message
from ai.tests.factories import make_conversation, make_message, make_user
from tenants.models import Tenant


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
        assert conv.tenant is None
        assert conv.tenant_id is None  # FK column accessor still works
        assert conv.deleted_at is None
        assert conv.last_message_at is None

    def test_tenant_fk_stored_and_retrieved(self):
        user = make_user()
        tenant = Tenant.objects.create(slug="formula", name="Формула тела")
        conv = make_conversation(user=user, tenant=tenant)
        conv.refresh_from_db()
        assert conv.tenant_id == tenant.id
        assert conv.tenant.slug == "formula"

    def test_tenant_nullable(self):
        user = make_user()
        conv = make_conversation(user=user, tenant=None)
        conv.refresh_from_db()
        assert conv.tenant is None

    def test_filter_by_tenant(self):
        # Each (user, tenant) pair must be unique among active rows
        # — see ai_conversation_one_active_per_user_tenant constraint.
        # So we use distinct users to seed the same tenant twice.
        t1 = Tenant.objects.create(slug="t1", name="T1")
        t2 = Tenant.objects.create(slug="t2", name="T2")
        u1, u2, u3 = make_user(), make_user(), make_user()
        make_conversation(user=u1, tenant=t1)
        make_conversation(user=u2, tenant=t1)
        make_conversation(user=u3, tenant=t2)
        make_conversation(user=u3, tenant=None)
        assert Conversation.objects.filter(tenant=t1).count() == 2

    def test_tenant_protect_blocks_delete_with_active_conversations(self):
        """PROTECT semantics: dropping a tenant must fail if conversations
        reference it — admin must reassign / soft-delete history first."""
        from django.db.models.deletion import ProtectedError
        user = make_user()
        tenant = Tenant.objects.create(slug="proto", name="P")
        make_conversation(user=user, tenant=tenant)
        with pytest.raises(ProtectedError):
            tenant.delete()

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


# ---------------------------------------------------------------------------
# DRF-240 deltas — soft-delete + active-uniqueness + analytics index
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSoftDelete:
    def test_default_manager_hides_deleted(self):
        user = make_user()
        keep = make_conversation(user=user)
        gone = make_conversation(user=user)
        # gone has is_active=False so the partial unique constraint
        # doesn't fire; we only test the manager filter here.
        gone.is_active = False
        gone.save(update_fields=["is_active"])
        gone.deleted_at = timezone.now()
        gone.save(update_fields=["deleted_at"])

        ids = set(Conversation.objects.values_list("id", flat=True))
        assert keep.id in ids
        assert gone.id not in ids

    def test_all_objects_includes_deleted(self):
        user = make_user()
        conv = make_conversation(user=user)
        conv.mark_deleted()
        assert Conversation.all_objects.filter(id=conv.id).exists()

    def test_mark_deleted_sets_fields(self):
        user = make_user()
        conv = make_conversation(user=user)
        assert conv.is_active is True
        conv.mark_deleted()
        conv.refresh_from_db()
        assert conv.is_active is False
        assert conv.deleted_at is not None


@pytest.mark.django_db
class TestActiveConversationUniqueness:
    def test_constraint_present_in_meta(self):
        names = [c.name for c in Conversation._meta.constraints]
        assert "ai_conversation_one_active_per_user_tenant" in names

    def test_two_active_same_user_same_tenant_rejected(self):
        user = make_user()
        tenant = Tenant.objects.create(slug="uniqctx", name="U")
        make_conversation(user=user, tenant=tenant, is_active=True)
        with pytest.raises(IntegrityError):
            make_conversation(user=user, tenant=tenant, is_active=True)

    def test_inactive_does_not_block_new_active(self):
        user = make_user()
        old = make_conversation(user=user, is_active=True)
        old.is_active = False
        old.save(update_fields=["is_active"])
        # Same user can have a new active conversation now.
        make_conversation(user=user, is_active=True)
        assert (
            Conversation.objects.filter(user=user, is_active=True).count() == 1
        )

    def test_soft_deleted_does_not_block_new_active(self):
        user = make_user()
        first = make_conversation(user=user)
        first.mark_deleted()
        # Soft-deleted row is is_active=False AND deleted_at IS NOT NULL,
        # so the partial unique condition doesn't match — new active OK.
        make_conversation(user=user, is_active=True)


@pytest.mark.django_db
class TestMessageActionTypeIndex:
    def test_role_action_type_index_present(self):
        index_fields = [tuple(idx.fields) for idx in Message._meta.indexes]
        assert ("role", "action_type") in index_fields
