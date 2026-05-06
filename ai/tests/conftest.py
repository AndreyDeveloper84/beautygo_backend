"""Shared fixtures for ai app tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rest_framework.test import APIClient

from ai.tests.factories import make_conversation, make_specialist, make_user


@pytest.fixture
def client_user(db):
    return make_user(role="client", is_guest=False, city="Penza")


@pytest.fixture
def guest_user(db):
    return make_user(role="client", is_guest=True)


@pytest.fixture
def specialist_a(db):
    return make_specialist(display_name="Анна", rating=4.9, reviews_count=80)


@pytest.fixture
def specialist_b(db):
    return make_specialist(display_name="Мария", rating=4.7, reviews_count=64)


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


@pytest.fixture
def guest_client(guest_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=guest_user)
    return c


@pytest.fixture
def conversation(client_user):
    return make_conversation(user=client_user)


def _completion(content: str = "OK", tool_calls=None, tokens_in=10, tokens_out=20):
    """Build a fake OpenAI ChatCompletion-like response."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out)
    return SimpleNamespace(choices=[choice], usage=usage)


def _tool_call(name: str, args: dict | str, call_id: str = "tc-1"):
    """Build a fake OpenAI tool_call object."""
    import json as _json

    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=_json.dumps(args) if isinstance(args, dict) else args,
        ),
    )


@pytest.fixture
def fake_completion():
    return _completion


@pytest.fixture
def fake_tool_call():
    return _tool_call


@pytest.fixture
def patch_openai(settings):
    """Patch the async OpenAI client used by AIConcierge for the chat path.

    AIConcierge calls ``await client.chat.completions.create(...)`` —
    the mocked ``create`` is an AsyncMock so tests can still set
    ``patch_openai.chat.completions.create.return_value`` (or
    ``side_effect``) the same way as before.

    Patches `ai.concierge_factory.get_async_openai_client` (DRF-241
    Slice B): the factory is the single seam where the OpenAI client
    enters the AIConcierge instance, so one patch covers all
    chat_service / concierge paths.
    """
    settings.OPENAI_API_KEY = "test-key"
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock()
    with patch(
        "ai.concierge_factory.get_async_openai_client",
        return_value=mock_client,
    ):
        yield mock_client
