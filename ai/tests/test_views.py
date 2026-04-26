"""Integration tests for AI Chat HTTP endpoints."""
from __future__ import annotations

import uuid as _uuid

import pytest
from rest_framework.test import APIClient

from ai.models import Message
from ai.tests.factories import (
    make_conversation,
    make_message,
    make_user,
)


pytestmark = pytest.mark.django_db


CHAT_URL = "/api/v1/ai/chat/"
CONVS_URL = "/api/v1/ai/conversations/"


def _action_url(conv_id):
    return f"/api/v1/ai/chat/{conv_id}/action/"


def _detail_url(conv_id):
    return f"/api/v1/ai/conversations/{conv_id}/"


# ---------------------------------------------------------------------------
# POST /chat/
# ---------------------------------------------------------------------------


class TestChatEndpoint:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(CHAT_URL, {"message": "hi"}, format="json")
        assert resp.status_code == 401

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(CHAT_URL, {"message": "hi"}, format="json")
        assert resp.status_code == 403

    def test_missing_app_type_returns_403(self, client_user):
        c = APIClient()
        c.force_authenticate(user=client_user)
        resp = c.post(CHAT_URL, {"message": "hi"}, format="json")
        assert resp.status_code == 403

    def test_empty_openai_key_returns_503(self, auth_client, settings):
        settings.OPENAI_API_KEY = ""
        resp = auth_client.post(
            CHAT_URL, {"message": "hi"}, format="json"
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "AI_UNAVAILABLE"

    def test_validation_error_for_empty_message(self, auth_client):
        resp = auth_client.post(CHAT_URL, {"message": ""}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_happy_path_creates_conversation_and_returns_envelope(
        self, auth_client, patch_openai, fake_completion, specialist_a
    ):
        patch_openai.chat.completions.create.return_value = fake_completion(
            content="Привет!"
        )
        resp = auth_client.post(
            CHAT_URL, {"message": "хочу маникюр"}, format="json"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        data = body["data"]
        assert "conversation_id" in data
        assert data["message"]["role"] == "assistant"
        assert data["message"]["content"] == "Привет!"


# ---------------------------------------------------------------------------
# POST /chat/{id}/action/
# ---------------------------------------------------------------------------


class TestActionEndpoint:
    def test_unknown_conversation_returns_404(self, auth_client):
        resp = auth_client.post(
            _action_url(_uuid.uuid4()),
            {"action_type": "ask_clarification", "confirmed": True,
             "data": {"answer": "yes"}},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"

    def test_other_users_conversation_returns_403(self, auth_client):
        other = make_user(role="client")
        conv = make_conversation(user=other)
        resp = auth_client.post(
            _action_url(conv.id),
            {"action_type": "ask_clarification", "confirmed": True,
             "data": {"answer": "x"}},
            format="json",
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "NOT_OWNER"

    def test_invalid_action_type_returns_400(self, auth_client, client_user):
        conv = make_conversation(user=client_user)
        resp = auth_client.post(
            _action_url(conv.id),
            {"action_type": "totally_made_up", "confirmed": True},
            format="json",
        )
        assert resp.status_code == 400

    def test_ask_clarification_records_user_message(
        self, auth_client, client_user
    ):
        conv = make_conversation(user=client_user)
        resp = auth_client.post(
            _action_url(conv.id),
            {"action_type": "ask_clarification", "confirmed": True,
             "data": {"answer": "вторник"}},
            format="json",
        )
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["success"] is True
        assert Message.objects.filter(
            conversation=conv, role=Message.Role.USER, content="вторник"
        ).exists()


# ---------------------------------------------------------------------------
# GET /conversations/
# ---------------------------------------------------------------------------


class TestConversationListEndpoint:
    def test_anonymous_returns_403(self, guest_client):
        resp = guest_client.get(CONVS_URL)
        assert resp.status_code == 403

    def test_returns_users_conversations_only(self, auth_client, client_user):
        mine = make_conversation(user=client_user)
        make_message(conversation=mine, content="мой первый")

        other = make_user(role="client")
        other_conv = make_conversation(user=other)
        make_message(conversation=other_conv, content="чужой")

        resp = auth_client.get(CONVS_URL)
        assert resp.status_code == 200
        data = resp.json()["data"]
        ids = [r["id"] for r in data["results"]]
        assert str(mine.id) in ids
        assert str(other_conv.id) not in ids

    def test_preview_truncates_to_first_user_message(
        self, auth_client, client_user
    ):
        conv = make_conversation(user=client_user)
        long = "Я хочу записаться на маникюр у того мастера которого вы советовали мне в прошлый раз "
        make_message(conversation=conv, content=long)
        resp = auth_client.get(CONVS_URL)
        item = resp.json()["data"]["results"][0]
        assert len(item["preview"]) <= 80


# ---------------------------------------------------------------------------
# GET / DELETE /conversations/{id}/
# ---------------------------------------------------------------------------


class TestConversationDetailEndpoint:
    def test_returns_embedded_messages(self, auth_client, client_user):
        conv = make_conversation(user=client_user)
        make_message(conversation=conv, content="привет")
        make_message(
            conversation=conv,
            role=Message.Role.ASSISTANT,
            content="здравствуйте",
            action_type="ask_clarification",
            action_data={"question": "что интересует?", "options": []},
        )

        resp = auth_client.get(_detail_url(conv.id))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["messages"]) == 2
        assistant = next(m for m in data["messages"] if m["role"] == "assistant")
        assert assistant["action"] == {
            "type": "ask_clarification",
            "data": {"question": "что интересует?", "options": []},
        }

    def test_other_users_detail_returns_404(self, auth_client):
        other = make_user(role="client")
        conv = make_conversation(user=other)
        resp = auth_client.get(_detail_url(conv.id))
        assert resp.status_code == 404

    def test_delete_soft_deletes(self, auth_client, client_user):
        conv = make_conversation(user=client_user)
        resp = auth_client.delete(_detail_url(conv.id))
        assert resp.status_code == 204
        conv.refresh_from_db()
        assert conv.is_active is False
        assert conv.deleted_at is not None
