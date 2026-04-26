"""Unit tests for OpenAIVisionProvider."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nutrition.providers.base import (
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
)
from nutrition.providers.openai_vision import OpenAIVisionProvider


def _completion(content: str):
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


@pytest.fixture
def patch_openai_client(settings):
    settings.OPENAI_API_KEY = "test-key"
    mock_client = MagicMock()
    with patch(
        "nutrition.providers.openai_vision.get_openai_client",
        return_value=mock_client,
    ):
        yield mock_client


class TestConfiguration:
    def test_missing_api_key_raises_unavailable(self, settings):
        settings.OPENAI_API_KEY = ""
        with pytest.raises(ProviderUnavailable):
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")


class TestSuccessPath:
    def test_high_confidence_returns_result(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({
                "dish_name": "Борщ",
                "confidence": 0.92,
                "portion_g": 320,
                "ingredients": ["свёкла", "капуста", "говядина"],
            })
        )
        result = OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert result.dish_name == "Борщ"
        assert result.confidence == 0.92
        assert result.portion_g == 320
        assert "свёкла" in result.ingredients
        assert result.provider == "openai"
        assert result.latency_ms >= 0

    def test_portion_multiplier_is_applied(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({"dish_name": "X", "confidence": 0.9, "portion_g": 100})
        )
        result = OpenAIVisionProvider().scan(
            b"\xff\xd8\xff", portion_multiplier=1.5,
        )
        assert result.portion_g == 150

    def test_null_portion_passes_through(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({"dish_name": "X", "confidence": 0.9, "portion_g": None})
        )
        result = OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert result.portion_g is None

    def test_image_passed_as_data_url(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({"dish_name": "X", "confidence": 0.9})
        )
        OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        call = patch_openai_client.chat.completions.create.call_args
        messages = call.kwargs["messages"]
        user_content = messages[-1]["content"]
        image_part = next(c for c in user_content if c["type"] == "image_url")
        assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


class TestLowConfidence:
    def test_low_confidence_raises(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({"dish_name": "??", "confidence": 0.2})
        )
        with pytest.raises(LowConfidenceError) as exc_info:
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        # Partial result attached for caller (router) to see what was rejected.
        assert exc_info.value.partial is not None
        assert exc_info.value.partial.confidence == 0.2

    def test_threshold_is_configurable(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps({"dish_name": "borderline", "confidence": 0.5})
        )
        provider = OpenAIVisionProvider(confidence_threshold=0.6)
        with pytest.raises(LowConfidenceError):
            provider.scan(b"\xff\xd8\xff")


class TestErrorHandling:
    def test_non_json_response_unavailable(self, patch_openai_client):
        patch_openai_client.chat.completions.create.return_value = _completion(
            "Это не JSON, gpt сорри"
        )
        with pytest.raises(ProviderUnavailable):
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")

    def test_timeout_classified_as_timeout(self, patch_openai_client):
        class APITimeoutError(Exception):
            pass

        patch_openai_client.chat.completions.create.side_effect = APITimeoutError(
            "timed out"
        )
        with pytest.raises(ProviderTimeout):
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")

    def test_generic_5xx_classified_as_unavailable(self, patch_openai_client):
        patch_openai_client.chat.completions.create.side_effect = RuntimeError(
            "internal server error"
        )
        with pytest.raises(ProviderUnavailable):
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")

    def test_array_json_treated_as_invalid(self, patch_openai_client):
        # Defensive: if model returns a list at the top level, we treat
        # it as an unparseable response rather than crashing on .get().
        patch_openai_client.chat.completions.create.return_value = _completion(
            json.dumps([{"dish_name": "x"}])
        )
        with pytest.raises(ProviderUnavailable):
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")
