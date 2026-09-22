"""Стойкий отказ распознавателя фото отличается от временного (DRF-2318).

Живой проход владельца 22.09 18:54: фото помидора → OpenAI 429
``billing_not_active`` («Your account is not active…»), резервный провайдер
тоже ``ProviderUnavailable`` → 503 ``FOOD_API_UNAVAILABLE`` → бот «попробуй
через минуту». Через минуту ничего не изменится: счёт не оплачен.

* p* — провайдеры отличают стойкий отказ (счёт не активен, ключ отвергнут,
  квота исчерпана, ключ не настроен) от временного (429 rate limit, 5xx,
  сеть) и называют причину закрытым словом;
* r* — роутер: стойкий, только если стойко отказали ВСЕ опрошенные;
* v* — ручка бота: 503 ``FOOD_API_UNAVAILABLE`` с ``details.permanent`` и
  ``details.reason`` — код прежний, прежние читатели не ломаются;
* s* — стойкий отказ провайдера → сигнал операторам
  ``system.module.health.degraded`` / ``nutrition.food_scan``, как сигнал
  бюджета, один на (провайдер, причина) за час; временный — без сигнала.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
from django.core.cache import cache

from appointments.models import OutboxEvent
from nutrition.providers.base import (
    LowConfidenceError,
    ProviderPermanentlyUnavailable,
    ProviderUnavailable,
    ScanResult,
)
from nutrition.providers.openai_vision import OpenAIVisionProvider
from nutrition.providers.yandex import YandexVisionProvider
from nutrition.services.food_scanner_router import AllProvidersFailedError, FoodScannerRouter
from nutrition.tests.test_food_scan_budget_2145 import (  # noqa: F401 — фикстуры по имени
    _post_scan,
    _router,
    _settings,
)

pytestmark = pytest.mark.django_db

TOPIC = "system.module.health.degraded"
_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _openai_error(cls, status: int, code: str):
    body = {"error": {"message": "Your account is not active", "type": "x", "code": code}}
    return cls(message=f"Error code: {status}", response=httpx.Response(status, request=_REQ), body=body)


@pytest.fixture
def openai_client(settings):
    settings.OPENAI_API_KEY = "test-key"  # pragma: allowlist secret
    client = MagicMock()
    with patch("nutrition.providers.openai_vision.get_openai_client", return_value=client):
        yield client


def _signals() -> list[OutboxEvent]:
    return [
        e for e in OutboxEvent.objects.filter(topic=TOPIC)
        if (e.payload.get("data") or e.payload).get("module_name") == "nutrition.food_scan"
        and "provider" in ((e.payload.get("data") or e.payload).get("metric") or {})
    ]


class TestP1OpenAI:
    @pytest.mark.parametrize(
        ("cls", "status", "code", "reason"),
        [
            (openai.RateLimitError, 429, "billing_not_active", "billing_not_active"),
            (openai.RateLimitError, 429, "insufficient_quota", "quota_exhausted"),
            (openai.AuthenticationError, 401, "invalid_api_key", "invalid_api_key"),
        ],
    )
    def test_permanent_refusals_are_named(self, openai_client, cls, status, code, reason) -> None:
        openai_client.chat.completions.create.side_effect = _openai_error(cls, status, code)
        with pytest.raises(ProviderPermanentlyUnavailable) as exc:
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert exc.value.reason == reason

    def test_a_rate_limit_is_temporary(self, openai_client) -> None:
        openai_client.chat.completions.create.side_effect = _openai_error(
            openai.RateLimitError, 429, "rate_limit_exceeded"
        )
        with pytest.raises(ProviderUnavailable) as exc:
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert not isinstance(exc.value, ProviderPermanentlyUnavailable)

    def test_no_key_is_not_configured(self, settings) -> None:
        settings.OPENAI_API_KEY = ""
        with pytest.raises(ProviderPermanentlyUnavailable) as exc:
            OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert exc.value.reason == "not_configured"


class TestP2Yandex:
    def test_no_key_is_not_configured(self, settings) -> None:
        settings.YANDEX_VISION_API_KEY = ""
        settings.YANDEX_VISION_FOLDER_ID = ""
        with pytest.raises(ProviderPermanentlyUnavailable) as exc:
            YandexVisionProvider().scan(b"\xff\xd8\xff")
        assert exc.value.reason == "not_configured"

    @pytest.mark.parametrize(("status", "permanent"), [(401, True), (403, True), (500, False)])
    def test_http_refusals(self, settings, status, permanent) -> None:
        settings.YANDEX_VISION_API_KEY = "k"  # pragma: allowlist secret
        settings.YANDEX_VISION_FOLDER_ID = "f"
        response = httpx.Response(status, text="denied", request=httpx.Request("POST", "https://x"))
        with patch("nutrition.providers.yandex.httpx.Client") as client_cls:
            client_cls.return_value.__enter__.return_value.post.return_value = response
            with pytest.raises(ProviderUnavailable) as exc:
                YandexVisionProvider().scan(b"\xff\xd8\xff")
        assert isinstance(exc.value, ProviderPermanentlyUnavailable) is permanent
        if permanent:
            assert exc.value.reason == "auth_rejected"


class _Fails:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __call__(self):
        return self

    def scan(self, *args, **kwargs):
        raise self.exc


class _Works:
    def __call__(self):
        return self

    def scan(self, *args, **kwargs):
        return ScanResult(dish_name="помидор", confidence=0.9, portion_g=120, provider="yandex")


def _router_with(primary, fallback) -> FoodScannerRouter:
    return FoodScannerRouter(
        primary_name="openai", fallback_name="yandex",
        registry={"openai": primary, "yandex": fallback},
    )


class TestR1Router:
    def test_permanent_only_when_every_provider_is(self) -> None:
        router = _router_with(
            _Fails(ProviderPermanentlyUnavailable("billing", reason="billing_not_active")),
            _Fails(ProviderPermanentlyUnavailable("no key", reason="not_configured")),
        )
        with pytest.raises(AllProvidersFailedError) as exc:
            router.scan(b"x")
        assert exc.value.permanent_reason == "billing_not_active"

    @pytest.mark.parametrize(
        "fallback_err", [ProviderUnavailable("5xx"), LowConfidenceError("low")]
    )
    def test_a_temporary_fallback_makes_it_temporary(self, fallback_err) -> None:
        router = _router_with(
            _Fails(ProviderPermanentlyUnavailable("billing", reason="billing_not_active")),
            _Fails(fallback_err),
        )
        with pytest.raises(AllProvidersFailedError) as exc:
            router.scan(b"x")
        assert exc.value.permanent_reason is None


class TestV1TheBotHearsPermanent:
    def test_permanent_is_a_503_with_a_reason(self) -> None:
        err = AllProvidersFailedError(
            ProviderPermanentlyUnavailable("billing", reason="billing_not_active"),
            ProviderPermanentlyUnavailable("no key", reason="not_configured"),
        )
        resp = _post_scan(_router(err))
        assert resp.status_code == 503
        error = resp.json()["error"]
        assert error["code"] == "FOOD_API_UNAVAILABLE"
        assert error["details"] == {"permanent": True, "reason": "billing_not_active"}

    def test_temporary_stays_as_it_was(self) -> None:
        err = AllProvidersFailedError(ProviderUnavailable("5xx"), ProviderUnavailable("5xx"))
        resp = _post_scan(_router(err))
        assert resp.status_code == 503
        error = resp.json()["error"]
        assert error["code"] == "FOOD_API_UNAVAILABLE"
        assert not (error.get("details") or {}).get("permanent")


class TestS1OperatorsHearIt:
    def test_one_signal_per_provider_reason_hour(self) -> None:
        cache.clear()
        permanent = ProviderPermanentlyUnavailable("billing", reason="billing_not_active")
        for _ in range(3):
            with pytest.raises(AllProvidersFailedError):
                _router_with(_Fails(permanent), _Fails(ProviderUnavailable("5xx"))).scan(b"x")

        (signal,) = _signals()
        data = signal.payload.get("data") or signal.payload
        assert data["severity"] == "error"
        assert data["metric"]["provider"] == "openai"
        assert data["metric"]["reason"] == "billing_not_active"
        assert "Your account" not in repr(signal.payload)  # текст провайдера не уходит

    def test_signal_even_when_the_fallback_saves_the_scan(self) -> None:
        cache.clear()
        permanent = ProviderPermanentlyUnavailable("billing", reason="billing_not_active")
        result = _router_with(_Fails(permanent), _Works()).scan(b"x")
        assert result.result.dish_name == "помидор"  # наличие: скан спасён резервом
        assert len(_signals()) == 1

    def test_a_temporary_failure_is_silent(self) -> None:
        cache.clear()
        result = _router_with(_Fails(ProviderUnavailable("5xx")), _Works()).scan(b"x")
        assert result.result.dish_name == "помидор"  # наличие: ход прошёл
        assert _signals() == []
