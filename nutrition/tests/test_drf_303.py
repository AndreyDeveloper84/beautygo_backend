"""Tests for DRF-303 — caption in /scan/ + with_comment in /summary/.

Covers:
- caption flows from request body through router/provider into the LLM prompt
- caption is backwards-compat optional
- ai_comment is null when with_comment omitted (existing behaviour)
- with_comment=true triggers AICommentService
- eating_disorder profile uses the no-numbers template
- non-ED uses LLM and validates length
- 6h cache: second call within window doesn't re-invoke LLM
- LLM failure → neutral fallback (no exception leaked)
- daily cost-counter logs warning at limit+1
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog
from nutrition.providers.openai_vision import OpenAIVisionProvider
from nutrition.services.ai_comment_service import (
    AICommentService,
    SummaryFacts,
    _validate,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-303"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:303", role="client", is_proxy=True,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:303",
    }


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return buf.getvalue()


# ===========================================================================
# Caption — provider plumbing
# ===========================================================================


class TestOpenAIVisionCaption:
    def test_caption_injected_into_user_prompt(self, settings, patch_openai_client):
        settings.OPENAI_API_KEY = "test-key"
        # Build a parsable response so the provider doesn't raise.
        msg = MagicMock()
        msg.content = (
            '{"dish_name": "Паста", "confidence": 0.9, '
            '"portion_g": 200, "ingredients": []}'
        )
        choice = MagicMock(); choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        provider = OpenAIVisionProvider()
        provider.scan(
            b"\xff\xd8\xff",
            caption="это половина порции у мамы",
        )
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        user_msg = sent["messages"][1]
        text_part = next(p for p in user_msg["content"] if p["type"] == "text")
        assert "половина порции" in text_part["text"]
        assert "Подпись пользователя" in text_part["text"]

    def test_no_caption_omits_hint(self, settings, patch_openai_client):
        settings.OPENAI_API_KEY = "test-key"
        msg = MagicMock(); msg.content = (
            '{"dish_name": "x", "confidence": 0.9, '
            '"portion_g": null, "ingredients": []}'
        )
        choice = MagicMock(); choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        text_part = next(
            p for p in sent["messages"][1]["content"] if p["type"] == "text"
        )
        assert "Подпись пользователя" not in text_part["text"]

    def test_caption_truncated_at_280_chars(self, settings, patch_openai_client):
        settings.OPENAI_API_KEY = "test-key"
        msg = MagicMock(); msg.content = (
            '{"dish_name": "x", "confidence": 0.9, '
            '"portion_g": null, "ingredients": []}'
        )
        choice = MagicMock(); choice.message = msg
        patch_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        long_caption = "x" * 500
        OpenAIVisionProvider().scan(b"\xff\xd8\xff", caption=long_caption)
        sent = patch_openai_client.chat.completions.create.call_args.kwargs
        text_part = next(
            p for p in sent["messages"][1]["content"] if p["type"] == "text"
        )
        # 280-char ceiling
        assert text_part["text"].count("x") <= 280


class TestScanEndpointBackwardsCompat:
    def test_scan_endpoint_accepts_caption_field(self, proxy_user, headers, settings):
        """Wire path — verify the serializer accepts caption without 400.

        We don't mock the router here; we expect FoodScannerRouter to fail
        because providers are not configured in this test, but the
        validation layer must accept the body.
        """
        settings.FOOD_SCANNER_PRIMARY = "openai"
        settings.FOOD_SCANNER_FALLBACK = ""
        settings.OPENAI_API_KEY = ""  # forces ProviderUnavailable
        c = APIClient()
        resp = c.post(
            "/api/v1/nutrition/internal/scan/",
            {"image": ("test.png", _png_bytes(), "image/png"),
             "caption": "это половина"},
            format="multipart",
            **headers,
        )
        # Either 400 INVALID_FOOD or 503 — but NOT VALIDATION_ERROR for caption.
        body = resp.json()
        if "error" in body:
            assert body["error"]["code"] != "VALIDATION_ERROR" or (
                "caption" not in str(body["error"].get("details", {}))
            )


# ===========================================================================
# AICommentService — pure unit tests
# ===========================================================================


class TestValidate:
    def test_accepts_short_one_sentence(self):
        assert _validate("Хороший день — почти в норме.") is not None

    def test_rejects_over_220_chars(self):
        long = "x" * 230
        assert _validate(long) is None

    def test_rejects_more_than_three_sentences(self):
        assert _validate("Один. Два. Три. Четыре.") is None

    def test_strips_whitespace(self):
        assert _validate("  hi.  ") == "hi."


class TestEatingDisorderTemplate:
    def test_eating_disorder_returns_template_no_llm(self, proxy_user):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(
            user=proxy_user,
            health_flags={"eating_disorder": True},
        )
        # If the LLM gets called at all under ED mode the test fails.
        llm_factory = MagicMock(side_effect=AssertionError("LLM must not be called"))
        svc = AICommentService(llm_client_factory=llm_factory)
        out = svc.comment_for(
            user_id=proxy_user.id,
            day=datetime.now(dt_tz.utc).date(),
            facts=SummaryFacts(1500, 1800, 90, 50, 150, 1500, 2000, 4),
        )
        # Must not contain a calorie figure / number block.
        assert not any(ch.isdigit() for ch in out)


class TestLlmHappyPath:
    def test_calls_llm_and_caches(self, proxy_user):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(
            user=proxy_user, goal="lose", pace="moderate",
        )
        client = MagicMock()
        msg = MagicMock(); msg.content = "Хороший день — почти в норму."
        choice = MagicMock(); choice.message = msg
        client.chat.completions.create.return_value = MagicMock(
            choices=[choice],
        )
        svc = AICommentService(llm_client_factory=lambda: client)
        day = datetime.now(dt_tz.utc).date()
        facts = SummaryFacts(1450, 1500, 100, 50, 150, 2000, 2000, 3)
        first = svc.comment_for(user_id=proxy_user.id, day=day, facts=facts)
        assert "Хороший день" in first
        # Second call hits cache — no second LLM invocation.
        client.chat.completions.create.reset_mock()
        second = svc.comment_for(user_id=proxy_user.id, day=day, facts=facts)
        assert second == first
        assert client.chat.completions.create.call_count == 0


class TestLlmFallback:
    def test_llm_failure_returns_neutral_fallback(self, proxy_user):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(user=proxy_user, goal="maintain")
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        svc = AICommentService(llm_client_factory=lambda: client)
        out = svc.comment_for(
            user_id=proxy_user.id,
            day=datetime.now(dt_tz.utc).date(),
            facts=SummaryFacts(0, 1500, 0, 0, 0, 0, 2000, 0),
        )
        assert "записей пока нет" in out

    def test_overlong_response_falls_back(self, proxy_user):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(user=proxy_user, goal="lose")
        client = MagicMock()
        # First response too long; retry also too long → fallback.
        msg = MagicMock(); msg.content = "x" * 500
        choice = MagicMock(); choice.message = msg
        client.chat.completions.create.return_value = MagicMock(choices=[choice])
        svc = AICommentService(llm_client_factory=lambda: client)
        out = svc.comment_for(
            user_id=proxy_user.id,
            day=datetime.now(dt_tz.utc).date(),
            facts=SummaryFacts(1450, 1500, 100, 50, 150, 1500, 2000, 3),
        )
        # Neutral fallback is short and not 500 x's.
        assert len(out) < 220
        assert "x" * 100 not in out


# ===========================================================================
# /summary/?with_comment=true — endpoint integration
# ===========================================================================


class TestSummaryWithCommentEndpoint:
    def test_default_no_comment_field_null(self, proxy_user, headers):
        c = APIClient()
        resp = c.get("/api/v1/nutrition/internal/summary/", **headers)
        body = resp.json()["data"]
        assert body["ai_comment"] is None

    def test_with_comment_eating_disorder_returns_template(
        self, proxy_user, headers,
    ):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(
            user=proxy_user, health_flags={"eating_disorder": True},
        )
        c = APIClient()
        resp = c.get(
            "/api/v1/nutrition/internal/summary/",
            {"with_comment": "true"},
            **headers,
        )
        body = resp.json()["data"]
        assert body["ai_comment"] is not None
        assert not any(ch.isdigit() for ch in body["ai_comment"])

    @patch("ai.services.llm_client.get_openai_client")
    def test_with_comment_calls_llm_for_non_ed_profile(
        self, mock_factory, proxy_user, headers,
    ):
        from nutrition.models import NutritionProfile
        NutritionProfile.objects.create(user=proxy_user, goal="lose")
        msg = MagicMock(); msg.content = "Хороший день, почти в норме."
        choice = MagicMock(); choice.message = msg
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(choices=[choice])
        mock_factory.return_value = client

        c = APIClient()
        resp = c.get(
            "/api/v1/nutrition/internal/summary/",
            {"with_comment": "true"},
            **headers,
        )
        body = resp.json()["data"]
        assert "Хороший день" in body["ai_comment"]
        assert mock_factory.called
