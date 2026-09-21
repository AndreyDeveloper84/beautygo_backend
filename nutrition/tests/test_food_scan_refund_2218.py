"""«Не еда» возвращает попытку дня (DRF-2218, §63 сканера; дочерний DRF-2145).

DRF-2145 списывал попытку ДО провайдера — и оставлял её списанной при любом
исходе. §63: на заведомо неподходящем фото личный дневной лимит не
тратится. Но вызов провайдера уже оплачен, поэтому возвращается ТОЛЬКО
личный счётчик; общий потолок (денежный учёт дня) и стоимость вызова не
стираются.

* n1 — ``FOOD_NOT_RECOGNIZED`` (все провайдеры ответили «низкая
  уверенность») → личный счётчик возвращён: при лимите 2 после двух «не еда»
  третий скан доходит до провайдера;
* n2 — общий счётчик НЕ возвращается: «не еда» тратит общий потолок (за
  вызов заплачено) — после 5 «не еда» при общем потолке 5 шестой скан → 503;
* n3 — стоимость «не еда» записывается: ``FoodScan.provider_usage`` /
  ``provider_cost_usd`` из ``partial`` обоих провайдеров, суточная сумма
  растёт;
* n4 — сбой провайдера (``FOOD_API_UNAVAILABLE``) попытку НЕ возвращает —
  ложный вход «возврат при любой ошибке» красный;
* n5 — смешанный исход (primary «не еда», fallback упал) — это 503, не
  «не еда»: попытка не возвращается;
* n6 — дешёвый отсев до провайдера без списания: файл, который не картинка /
  больше 10 МиБ / чужой тип, отказывается валидацией (400) ДО бюджета —
  счётчики пусты, провайдер не вызван;
* n7 — возврат не уводит счётчик ниже нуля (протухший/выметенный ключ).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import LowConfidenceError, ProviderUnavailable, ScanResult
from nutrition.services import food_scan_budget as budget
from nutrition.services.food_scanner_router import AllProvidersFailedError, RouterResult
from users.models import User

pytestmark = pytest.mark.django_db

INTERNAL_URL = "/api/v1/nutrition/internal/scan/"
SERVICE_TOKEN = "test-service-token-DRF-2218"  # pragma: allowlist secret
_DAY = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(settings, monkeypatch):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN
    settings.FOOD_SCAN_DAILY_PER_USER = 2
    settings.FOOD_SCAN_DAILY_TOTAL = 5
    settings.FOOD_SCAN_PRICE_INPUT_USD_PER_1M = "2.50"
    settings.FOOD_SCAN_PRICE_OUTPUT_USD_PER_1M = "10.00"
    cache.clear()
    monkeypatch.setattr(budget, "_now", lambda: _DAY)
    monkeypatch.setattr(budget, "_send_signal", lambda *a, **kw: True)


def _jpeg() -> bytes:
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _gif() -> bytes:
    """Настоящий GIF: Django ``ImageField`` берёт тип из формата, а не из
    заголовка клиента — JPEG с заголовком image/gif честно JPEG."""
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


def _partial(tokens: int = 1000) -> ScanResult:
    return ScanResult(
        dish_name="?",
        confidence=0.1,
        portion_g=None,
        provider="openai",
        usage={"prompt_tokens": tokens, "completion_tokens": 100},
    )


def _not_food() -> AllProvidersFailedError:
    return AllProvidersFailedError(
        LowConfidenceError("low", partial=_partial(1000)),
        LowConfidenceError("low", partial=_partial(2000)),
    )


def _router(outcome) -> MagicMock:
    router = MagicMock()
    if isinstance(outcome, Exception):
        router.scan.side_effect = outcome
    else:
        router.scan.return_value = RouterResult(result=outcome, primary_provider_name="openai")
    return router


def _post(router: MagicMock, external_id: str = "bot:2218", upload=None):
    upload = upload or SimpleUploadedFile("meal.jpg", _jpeg(), content_type="image/jpeg")
    with (
        patch("nutrition.views.FoodScannerRouter", return_value=router),
        patch("nutrition.views.NutritionLookup", return_value=MagicMock(lookup=lambda *a, **kw: None)),
    ):
        return APIClient().post(
            INTERNAL_URL,
            {"image": upload},
            format="multipart",
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
            HTTP_X_EXTERNAL_USER_ID=external_id,
        )


def _user_counter(external_id: str = "bot:2218"):
    user = User.objects.get(username=external_id)
    return cache.get(f"food_scan:user:{user.pk}:2026-09-21")


class TestN1NotFoodRefundsThePersonalAttempt:
    def test_two_not_food_then_a_real_scan_reaches_the_provider(self) -> None:
        router = _router(_not_food())
        assert _post(router).status_code == 400
        assert _post(router).status_code == 400
        assert _user_counter() == 0
        ok = _router(ScanResult(dish_name="Борщ", confidence=0.9, portion_g=300, provider="openai"))
        assert _post(ok).status_code == 200
        assert _post(ok).status_code == 200
        assert _post(ok).status_code == 429  # лимит 2 — теперь честно из двух настоящих


class TestN2TotalIsNotRefunded:
    def test_not_food_still_spends_the_total_budget(self, settings) -> None:
        settings.FOOD_SCAN_DAILY_PER_USER = 100
        router = _router(_not_food())
        for i in range(5):
            assert _post(router, f"bot:{i}").status_code == 400
        assert cache.get("food_scan:total:2026-09-21") == 5
        assert _post(router, "bot:99").status_code == 503


class TestN3CostOfNotFoodIsKept:
    def test_usage_of_both_providers_is_recorded(self) -> None:
        _post(_router(_not_food()))
        scan = FoodScan.objects.get()
        assert scan.error_code == "FOOD_NOT_RECOGNIZED"
        assert scan.provider_usage == {"prompt_tokens": 3000, "completion_tokens": 200}
        # 3000 × 2.50 + 200 × 10.00 = 9500 / 1e6
        assert scan.provider_cost_usd == Decimal("0.009500")
        assert cache.get("food_scan:cost_usd:2026-09-21") == "0.009500"


class TestN4ProviderFailureIsNotRefunded:
    def test_unavailable_keeps_the_attempt_spent(self) -> None:
        router = _router(AllProvidersFailedError(ProviderUnavailable("down"), None))
        assert _post(router).status_code == 503
        assert _user_counter() == 1
        assert cache.get("food_scan:total:2026-09-21") == 1


class TestN5MixedIsA503:
    def test_low_confidence_plus_failure_is_not_refunded(self) -> None:
        router = _router(
            AllProvidersFailedError(
                LowConfidenceError("low", partial=_partial()), ProviderUnavailable("down")
            )
        )
        assert _post(router).status_code == 503
        assert _user_counter() == 1


class TestN6CheapRejectBeforeTheBudget:
    @pytest.mark.parametrize(
        "upload",
        [
            SimpleUploadedFile("meal.jpg", b"not an image at all", content_type="image/jpeg"),
            SimpleUploadedFile("meal.gif", _gif(), content_type="image/gif"),
            SimpleUploadedFile("meal.jpg", b"\xff\xd8" + b"0" * (10 * 1024 * 1024 + 1), content_type="image/jpeg"),
        ],
        ids=["not-an-image", "foreign-type", "over-10-mib"],
    )
    def test_invalid_file_is_400_and_spends_nothing(self, upload) -> None:
        router = _router(ScanResult(dish_name="Борщ", confidence=0.9, portion_g=300, provider="openai"))
        resp = _post(router, upload=upload)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert router.scan.call_count == 0
        assert cache.get("food_scan:total:2026-09-21") is None
        assert FoodScan.objects.count() == 0


class TestN7RefundNeverGoesNegative:
    def test_refund_on_a_missing_key_is_a_no_op(self) -> None:
        user = User.objects.create(username="bot:22180", role="client", is_proxy=True)
        budget.refund_personal(user)
        assert cache.get(f"food_scan:user:{user.pk}:2026-09-21") in (None, 0)

    def test_refund_after_reserve_is_zero(self) -> None:
        user = User.objects.create(username="bot:22181", role="client", is_proxy=True)
        budget.reserve(user)
        budget.refund_personal(user)
        budget.refund_personal(user)  # повтор — не ниже нуля
        assert cache.get(f"food_scan:user:{user.pk}:2026-09-21") == 0
        assert cache.get("food_scan:total:2026-09-21") == 1
