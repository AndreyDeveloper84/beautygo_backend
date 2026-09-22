"""Бюджет распознавания фото (DRF-2145, Сканер-1, В1 — до пилота).

Ни счётчика, ни бюджета у сканера не было: throttle 60/min — защита от
всплеска, не от объёма; расход провайдера не измерялся. Здесь — счётчики по
дню (UTC) на человека и общий, отказ словами при исчерпании, сигнал на 80 /
100 % общего потолка, стоимость из ``usage`` ответа провайдера.

* b1 — 21-й скан за день (личный потолок ``FOOD_SCAN_DAILY_PER_USER``) →
  429 ``FOOD_SCAN_DAILY_LIMIT`` с ``retry_after`` до полуночи UTC;
  провайдер (роутер) не вызван; строка ``FoodScan`` не создана — отказ до
  записи;
* b2 — общий потолок ``FOOD_SCAN_DAILY_TOTAL`` → 503
  ``FOOD_SCAN_BUDGET_EXHAUSTED``; провайдер не вызван; личный потолок ещё не
  достигнут;
* b3 — после полуночи (следующий день UTC) — снова 200: ключи по дню;
* b4 — текстовая оценка (``food-estimate``) при исчерпании — 200: бюджет её
  не ограничивает;
* b5 — попытка считается, не успех: провайдер упал → счётчик уже
  инкрементирован (ложный вход «инкремент после провайдера» — красный);
* b6 — сигнал операторам: 80 % общего → один ``warning`` за день, 100 % →
  один ``error`` за день (dedup по дню); в сообщении — сумма стоимости за
  день; менеджеру салона — ничего;
* b7 — стоимость: ``usage`` в ответе провайдера + цены в настройках →
  ``FoodScan.provider_cost_usd``; без ``usage`` или без цен — ``null`` (не
  0); ``provider_usage`` хранит токены как пришли;
* b8 — публичный ``/nutrition/scan/`` считается тем же счётчиком (ключ — по
  пользователю, не по каналу);
* b9 — сервис: ключи ``food_scan:user:<id>:<date>`` / ``food_scan:total:<date>``,
  TTL до полуночи; отказ по личному потолку не инкрементирует общий (и
  наоборот — общий отказ не тратит личный).
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import ScanResult
from nutrition.services import food_scan_budget as budget
from nutrition.services.food_scanner_router import AllProvidersFailedError, RouterResult
from users.models import User

pytestmark = pytest.mark.django_db

INTERNAL_URL = "/api/v1/nutrition/internal/scan/"
ESTIMATE_URL = "/api/v1/nutrition/internal/food-estimate/"
PUBLIC_URL = "/api/v1/nutrition/scan/"
SERVICE_TOKEN = "test-service-token-DRF-2145"

_DAY = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
_NEXT_DAY = datetime(2026, 9, 22, 0, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.FOOD_SCAN_DAILY_PER_USER = 2
    settings.FOOD_SCAN_DAILY_TOTAL = 5
    settings.FOOD_SCAN_PRICE_INPUT_USD_PER_1M = None
    settings.FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M = None
    cache.clear()


@pytest.fixture
def now(monkeypatch):
    holder = {"now": _DAY}
    monkeypatch.setattr(budget, "_now", lambda: holder["now"])
    return holder


def _image_bytes() -> bytes:
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _upload() -> SimpleUploadedFile:
    return SimpleUploadedFile("meal.jpg", _image_bytes(), content_type="image/jpeg")


def _scan_result(usage: dict | None = None) -> ScanResult:
    return ScanResult(
        dish_name="Борщ",
        confidence=0.9,
        portion_g=300,
        ingredients=["свёкла"],
        provider="openai",
        latency_ms=400,
        raw_response={"ok": True},
        usage=dict(usage or {}),
    )


def _router(result: ScanResult | Exception | None = None) -> MagicMock:
    router = MagicMock()
    if isinstance(result, Exception):
        router.scan.side_effect = result
    else:
        router.scan.return_value = RouterResult(
            result=result or _scan_result(), primary_provider_name="openai"
        )
    return router


def _post_scan(router: MagicMock, external_id: str = "bot:42"):
    c = APIClient()
    with (
        patch("nutrition.views.FoodScannerRouter", return_value=router),
        patch("nutrition.views.build_nutrition_lookup", return_value=MagicMock(lookup=lambda *a, **kw: None)),
    ):
        return c.post(
            INTERNAL_URL,
            {"image": _upload()},
            format="multipart",
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID=external_id,
        )


def _post_estimate():
    c = APIClient()
    c.credentials(HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN)
    return c.post(ESTIMATE_URL, {"dish_name": "борщ", "portion_g": 300}, format="json")


class TestB1PersonalLimit:
    def test_third_scan_is_429_and_the_provider_is_not_called(self, now) -> None:
        router = _router()
        assert _post_scan(router).status_code == 200
        assert _post_scan(router).status_code == 200
        assert router.scan.call_count == 2
        resp = _post_scan(router)
        assert resp.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        err = resp.json()["error"]
        assert err["code"] == "FOOD_SCAN_DAILY_LIMIT"
        assert err["details"]["retry_after"] == 14 * 3600  # 10:00 → 00:00 UTC
        assert err["details"]["limit"] == 2
        assert router.scan.call_count == 2
        assert FoodScan.objects.count() == 2  # отказ — до записи строки

    def test_another_person_is_not_affected(self, now) -> None:
        router = _router()
        _post_scan(router, "bot:1")
        _post_scan(router, "bot:1")
        assert _post_scan(router, "bot:1").status_code == 429
        assert _post_scan(router, "bot:2").status_code == 200


class TestB2TotalBudget:
    def test_sixth_scan_of_the_day_is_503(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 100
        router = _router()
        for i in range(5):
            assert _post_scan(router, f"bot:{i}").status_code == 200
        resp = _post_scan(router, "bot:99")
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert resp.json()["error"]["code"] == "FOOD_SCAN_BUDGET_EXHAUSTED"
        assert router.scan.call_count == 5


class TestB3Midnight:
    def test_next_day_is_200_again(self, now) -> None:
        router = _router()
        _post_scan(router)
        _post_scan(router)
        assert _post_scan(router).status_code == 429
        now["now"] = _NEXT_DAY
        assert _post_scan(router).status_code == 200


class TestB4TextPathIsNotBudgeted:
    def test_estimate_at_exhaustion_is_200(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 100
        settings.FOOD_SCAN_DAILY_TOTAL = 1
        router = _router()
        assert _post_scan(router).status_code == 200
        assert _post_scan(router).status_code == 503
        resp = _post_estimate()
        assert resp.status_code == 200, resp.json()


class TestB5AttemptCounts:
    def test_provider_failure_still_spends_the_attempt(self, now) -> None:
        """Ложный вход: инкремент ПОСЛЕ провайдера — красный."""
        from nutrition.providers.base import ProviderUnavailable

        failing = _router(AllProvidersFailedError(ProviderUnavailable("down"), None))
        assert _post_scan(failing).status_code == 503
        assert _post_scan(failing).status_code == 503
        assert failing.scan.call_count == 2
        # Две попытки съели личный потолок 2 — третья не доходит до провайдера.
        resp = _post_scan(failing)
        assert resp.status_code == 429
        assert failing.scan.call_count == 2


class TestB6OperatorSignal:
    def test_80_and_100_percent_signal_once_a_day_each(self, now, settings, caplog) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 100
        router = _router()
        with (
            patch.object(budget, "_send_signal", return_value=True) as sender,
            caplog.at_level(logging.INFO),
        ):
            for i in range(5):
                _post_scan(router, f"bot:{i}")
            _post_scan(router, "bot:extra")  # 503, второй раз 100 % — без повтора
            _post_scan(router, "bot:extra2")
        levels = [c.kwargs.get("level") or c.args[0] for c in sender.call_args_list]
        assert levels == ["warning", "error"]
        messages = [str(c.args[-1]) if c.args else str(c.kwargs.get("message")) for c in sender.call_args_list]
        assert "4/5" in messages[0] and "5/5" in messages[1]
        assert all("usd" in m.lower() for m in messages)
        # Лог — без идентификатора человека (и строки view об отказе тоже).
        ours = [r for r in caplog.records if r.name.startswith("nutrition.")]
        assert any("budget" in r.getMessage() for r in ours)  # присутствие
        assert not any("bot:" in r.getMessage() for r in ours)

    def test_signal_failure_does_not_break_the_scan(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 100
        settings.FOOD_SCAN_DAILY_TOTAL = 1
        router = _router()
        with patch.object(budget, "_send_signal", side_effect=RuntimeError("sentry down")):
            assert _post_scan(router).status_code == 200


class TestB7Cost:
    def test_cost_from_usage_and_prices(self, now, settings) -> None:
        settings.FOOD_SCAN_PRICE_INPUT_USD_PER_1M = "2.50"
        settings.FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M = "10.00"
        router = _router(_scan_result({"prompt_tokens": 1000, "completion_tokens": 100}))
        assert _post_scan(router).status_code == 200
        scan = FoodScan.objects.get()
        assert scan.provider_usage == {"prompt_tokens": 1000, "completion_tokens": 100}
        assert scan.provider_cost_usd == Decimal("0.003500")

    def test_no_usage_is_null_not_zero(self, now, settings) -> None:
        settings.FOOD_SCAN_PRICE_INPUT_USD_PER_1M = "2.50"
        settings.FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M = "10.00"
        router = _router(_scan_result())
        assert _post_scan(router).status_code == 200
        scan = FoodScan.objects.get()
        assert scan.provider_cost_usd is None
        assert scan.provider_usage == {}

    def test_no_prices_is_null_with_usage_kept(self, now) -> None:
        router = _router(_scan_result({"prompt_tokens": 1000, "completion_tokens": 100}))
        assert _post_scan(router).status_code == 200
        scan = FoodScan.objects.get()
        assert scan.provider_cost_usd is None
        assert scan.provider_usage["prompt_tokens"] == 1000


class TestB8PublicPathSharesTheCounter:
    def test_public_scan_counts_for_the_same_user(self, now) -> None:
        proxy = User.objects.create(username="bot:77", role="client", is_proxy=True)
        router = _router()
        assert _post_scan(router, "bot:77").status_code == 200
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=proxy)
        with (
            patch("nutrition.views.FoodScannerRouter", return_value=router),
            patch("nutrition.views.build_nutrition_lookup", return_value=MagicMock(lookup=lambda *a, **kw: None)),
        ):
            second = c.post(PUBLIC_URL, {"image": _upload()}, format="multipart")
            third = c.post(PUBLIC_URL, {"image": _upload()}, format="multipart")
        assert second.status_code == 200
        assert third.status_code == 429


class TestB9Service:
    def test_keys_and_ttl(self, now) -> None:
        user = User.objects.create(username="bot:5", role="client", is_proxy=True)
        budget.reserve(user)
        assert cache.get(f"food_scan:user:{user.pk}:2026-09-21") == 1
        assert cache.get("food_scan:total:2026-09-21") == 1
        assert budget.seconds_until_midnight() == 14 * 3600

    def test_personal_refusal_does_not_spend_the_total(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 1
        user = User.objects.create(username="bot:6", role="client", is_proxy=True)
        budget.reserve(user)
        with pytest.raises(budget.DailyLimitExceeded):
            budget.reserve(user)
        assert cache.get("food_scan:total:2026-09-21") == 1

    def test_total_refusal_does_not_spend_the_personal(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_TOTAL = 1
        a = User.objects.create(username="bot:7", role="client", is_proxy=True)
        b = User.objects.create(username="bot:8", role="client", is_proxy=True)
        budget.reserve(a)
        with pytest.raises(budget.BudgetExhausted):
            budget.reserve(b)
        assert cache.get(f"food_scan:user:{b.pk}:2026-09-21") in (None, 0)


# ─── ревью #519 ───────────────────────────────────────────────────────────


class TestR1ProviderUsageWiring:
    def test_openai_result_carries_usage_end_to_end(self, settings) -> None:
        """Блокер ревью: ``ScanResult`` — frozen dataclass; присваивание
        ``usage`` роняло каждый успешный скан. Здесь — провайдер целиком."""
        import json
        from types import SimpleNamespace

        from nutrition.providers.openai_vision import OpenAIVisionProvider

        settings.OPENAI_API_KEY = "test-key"  # pragma: allowlist secret
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {"dish_name": "Борщ", "confidence": 0.92, "portion_g": 320, "ingredients": []}
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        with patch("nutrition.providers.openai_vision.get_openai_client", return_value=client):
            result = OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert result.dish_name == "Борщ"
        assert result.usage == {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}

    def test_no_usage_on_the_completion_is_an_empty_dict(self, settings) -> None:
        import json
        from types import SimpleNamespace

        from nutrition.providers.openai_vision import OpenAIVisionProvider

        settings.OPENAI_API_KEY = "test-key"  # pragma: allowlist secret
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({"dish_name": "Борщ", "confidence": 0.92, "portion_g": 320})
                    )
                )
            ]
        )
        client = MagicMock()
        client.chat.completions.create.return_value = completion
        with patch("nutrition.providers.openai_vision.get_openai_client", return_value=client):
            result = OpenAIVisionProvider().scan(b"\xff\xd8\xff")
        assert result.dish_name == "Борщ"
        assert result.usage == {}


class TestR2FailOpen:
    def test_cache_returning_none_does_not_refuse(self, now, caplog) -> None:
        """django_redis с IGNORE_EXCEPTIONS при лежащем Redis отдаёт None."""
        user = User.objects.create(username="bot:9", role="client", is_proxy=True)
        fake = MagicMock()
        fake.add.return_value = None
        fake.incr.return_value = None
        with patch.object(budget, "cache", fake), caplog.at_level(logging.WARNING):
            for _ in range(30):
                budget.reserve(user)  # ни одного отказа
        assert any("budget_unavailable" in r.getMessage() for r in caplog.records)

    def test_incr_on_a_missing_key_is_fail_open_and_loud(self, now, caplog) -> None:
        user = User.objects.create(username="bot:10", role="client", is_proxy=True)
        fake = MagicMock()
        fake.add.return_value = True
        fake.incr.side_effect = ValueError("key missing")
        with patch.object(budget, "cache", fake), caplog.at_level(logging.WARNING):
            budget.reserve(user)
        assert any("err=ValueError" in r.getMessage() for r in caplog.records)


class TestR3TtlIsToMidnight:
    def test_counters_and_dedup_keys_get_the_midnight_ttl(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_TOTAL = 1
        user = User.objects.create(username="bot:11", role="client", is_proxy=True)
        with patch.object(budget.cache, "add", wraps=budget.cache.add) as add:
            with patch.object(budget, "_send_signal", return_value=True):
                budget.reserve(user)
        timeouts = {c.args[0]: c.kwargs.get("timeout") for c in add.call_args_list}
        assert timeouts[f"food_scan:user:{user.pk}:2026-09-21"] == 14 * 3600
        assert timeouts["food_scan:total:2026-09-21"] == 14 * 3600
        assert timeouts["food_scan:signal:2026-09-21:error"] == 14 * 3600


class TestR4SignalNeverBreaksTheScan:
    def test_garbage_cost_value_is_na(self, now, settings) -> None:
        settings.FOOD_SCAN_DAILY_TOTAL = 1
        cache.set("food_scan:cost_usd:2026-09-21", "garbage", timeout=3600)
        router = _router()
        with patch.object(budget, "_send_signal", return_value=True) as sender:
            assert _post_scan(router).status_code == 200
        assert "cost_usd=n/a" in sender.call_args.args[-1]

    def test_negative_price_is_ignored(self, now, settings) -> None:
        settings.FOOD_SCAN_PRICE_INPUT_USD_PER_1M = "-2.50"
        settings.FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M = "10.00"
        assert budget.cost_usd({"prompt_tokens": 1000, "completion_tokens": 100}) is None
