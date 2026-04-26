"""Shared fixtures for nutrition tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def patch_openai_client(settings):
    """Patches the OpenAI client constructor used by OpenAIVisionProvider.

    Returns a Mock client — set
    ``mock.chat.completions.create.return_value`` per test.
    """
    settings.OPENAI_API_KEY = "test-key"
    mock_client = MagicMock()
    with patch(
        "nutrition.providers.openai_vision.get_openai_client",
        return_value=mock_client,
    ):
        yield mock_client
