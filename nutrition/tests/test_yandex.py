"""Unit tests for YandexVisionProvider."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nutrition.providers.base import (
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
)
from nutrition.providers.yandex import YandexVisionProvider


def _yandex_body(content_text: str) -> dict:
    return {
        "result": {
            "alternatives": [
                {"message": {"role": "assistant", "text": content_text}}
            ]
        }
    }


def _ok_response(content_text: str, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = _yandex_body(content_text)
    response.text = content_text
    return response


@pytest.fixture
def configured(settings):
    settings.YANDEX_VISION_API_KEY = "test-key"
    settings.YANDEX_VISION_FOLDER_ID = "b1g-folder-test"


class TestConfiguration:
    def test_missing_api_key_raises_unavailable(self, settings):
        settings.YANDEX_VISION_API_KEY = ""
        settings.YANDEX_VISION_FOLDER_ID = "b1g-folder"
        with pytest.raises(ProviderUnavailable):
            YandexVisionProvider().scan(b"\xff\xd8\xff")

    def test_missing_folder_raises_unavailable(self, settings):
        settings.YANDEX_VISION_API_KEY = "test-key"
        settings.YANDEX_VISION_FOLDER_ID = ""
        with pytest.raises(ProviderUnavailable):
            YandexVisionProvider().scan(b"\xff\xd8\xff")


class TestSuccessPath:
    def test_high_confidence_returns_result(self, configured):
        body = json.dumps({
            "dish_name": "Оливье",
            "confidence": 0.88,
            "portion_g": 250,
            "ingredients": ["картофель", "майонез", "колбаса"],
        })
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=_ok_response(body)),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            result = YandexVisionProvider().scan(b"\xff\xd8\xff")
        assert result.dish_name == "Оливье"
        assert result.confidence == 0.88
        assert result.portion_g == 250
        assert result.provider == "yandex"

    def test_request_includes_authorization_and_folder_id(self, configured):
        body = json.dumps({"dish_name": "X", "confidence": 0.9})
        post_mock = MagicMock(return_value=_ok_response(body))
        client_inst = MagicMock(post=post_mock)
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=client_inst),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            YandexVisionProvider().scan(b"\xff\xd8\xff")
        call = post_mock.call_args
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Api-Key test-key"
        assert headers["x-folder-id"] == "b1g-folder-test"
        # Payload references the folder in modelUri:
        assert "b1g-folder-test" in call.kwargs["json"]["modelUri"]


class TestLowConfidence:
    def test_low_confidence_raises(self, configured):
        body = json.dumps({"dish_name": "??", "confidence": 0.2})
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=_ok_response(body)),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(LowConfidenceError) as exc_info:
                YandexVisionProvider().scan(b"\xff\xd8\xff")
        assert exc_info.value.partial is not None


class TestErrorHandling:
    def test_5xx_raises_unavailable(self, configured):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503
        response.text = "service unavailable"
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=response),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(ProviderUnavailable):
                YandexVisionProvider().scan(b"\xff\xd8\xff")

    def test_4xx_raises_unavailable(self, configured):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 401
        response.text = "invalid api key"
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=response),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(ProviderUnavailable):
                YandexVisionProvider().scan(b"\xff\xd8\xff")

    def test_timeout_raises_provider_timeout(self, configured):
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(side_effect=httpx.ReadTimeout("timed out")),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(ProviderTimeout):
                YandexVisionProvider().scan(b"\xff\xd8\xff")

    def test_non_json_text_unavailable(self, configured):
        body = "это не JSON извини"
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=_ok_response(body)),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(ProviderUnavailable):
                YandexVisionProvider().scan(b"\xff\xd8\xff")

    def test_empty_alternatives_unavailable(self, configured):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {"result": {"alternatives": []}}
        response.text = '{"result": {"alternatives": []}}'
        with patch(
            "nutrition.providers.yandex.httpx.Client",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock(
                    post=MagicMock(return_value=response),
                )),
                __exit__=MagicMock(return_value=False),
            ),
        ):
            with pytest.raises(ProviderUnavailable):
                YandexVisionProvider().scan(b"\xff\xd8\xff")
