"""Стойкий отказ распознавателя возвращает попытку дня (DRF-2322).

DRF-2145 списывает попытку ДО вызова провайдера. DRF-2218 возвращает ЛИЧНУЮ
попытку на «не еда»: вызов был оплачен, поэтому общий потолок остаётся. А
стойкий отказ (DRF-2318: ``billing_not_active``, ``quota_exhausted``,
``invalid_api_key``, ``auth_rejected``, ``not_configured``) не возвращал
ничего — человек терял попытку за нашу поломку.

Правило: стойкий отказ → личная попытка возвращается всегда; общий потолок —
по факту оплаченных вызовов. ``permanent_reason`` выставляется, только когда
ВСЕ опрошенные провайдеры отказали стойко, а такие отказы не несут частичного
результата (``partial``), то есть оплаченного вызова не было — общий потолок
возвращается тоже. Смешанный исход (хоть один временный отказ или «низкая
уверенность») — ``permanent_reason`` пуст, поведение прежнее.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache

from nutrition.providers.base import (
    LowConfidenceError,
    ProviderPermanentlyUnavailable,
    ProviderUnavailable,
)
from nutrition.services.food_scanner_router import AllProvidersFailedError
from nutrition.tests.test_food_scan_refund_2218 import (  # noqa: F401 — фикстура по имени
    _partial,
    _post,
    _router,
    _settings,
    _user_counter,
)

pytestmark = pytest.mark.django_db

TOTAL_KEY = "food_scan:total:2026-09-21"


def _permanent(reason: str = "billing_not_active") -> ProviderPermanentlyUnavailable:
    return ProviderPermanentlyUnavailable("счёт не активен", reason=reason)


def _both_permanent(reason: str = "billing_not_active") -> AllProvidersFailedError:
    return AllProvidersFailedError(_permanent(reason), _permanent(reason))


class TestAPersistentRefusalGivesTheAttemptBack:
    def test_the_personal_attempt_and_the_total_are_returned(self) -> None:
        """Вызова не было (счёт не активен) — ни личная попытка, ни потолок не тратятся."""
        resp = _post(_router(_both_permanent()))

        assert resp.status_code == 503
        assert resp.json()["error"]["details"] == {
            "permanent": True,
            "reason": "billing_not_active",
        }
        assert _user_counter() == 0
        assert cache.get(TOTAL_KEY) == 0

    def test_a_single_provider_refusal_counts_too(self) -> None:
        """Резерв не опрашивался — «все опрошенные» это один провайдер."""
        resp = _post(_router(AllProvidersFailedError(_permanent("not_configured"), None)))

        assert resp.status_code == 503
        assert _user_counter() == 0
        assert cache.get(TOTAL_KEY) == 0

    def test_the_day_is_not_spent_by_our_own_breakage(self) -> None:
        """Личный лимит 2: два стойких отказа подряд не съедают день."""
        router = _router(_both_permanent())
        assert _post(router).status_code == 503
        assert _post(router).status_code == 503
        assert _user_counter() == 0

    def test_the_refund_never_goes_below_zero(self) -> None:
        router = _router(_both_permanent())
        _post(router)
        _post(router)
        assert _user_counter() == 0
        assert cache.get(TOTAL_KEY) == 0


class TestEverythingElseIsUnchanged:
    def test_a_transient_failure_still_spends_the_attempt(self) -> None:
        """Временный отказ — как было (через минуту может получиться)."""
        resp = _post(_router(AllProvidersFailedError(ProviderUnavailable("down"), None)))

        assert resp.status_code == 503
        assert _user_counter() == 1
        assert cache.get(TOTAL_KEY) == 1

    def test_a_mixed_outcome_is_transient(self) -> None:
        """Один стойкий, один временный — отказ временный: возврата нет."""
        resp = _post(
            _router(AllProvidersFailedError(_permanent(), ProviderUnavailable("down")))
        )

        assert resp.status_code == 503
        assert resp.json()["error"].get("details") is None
        assert _user_counter() == 1

    def test_not_food_still_refunds_only_the_personal_attempt(self) -> None:
        """Регрессия DRF-2218: за «не еда» вызов оплачен — потолок остаётся."""
        resp = _post(
            _router(
                AllProvidersFailedError(
                    LowConfidenceError("low", partial=_partial(1000)),
                    LowConfidenceError("low", partial=_partial(2000)),
                )
            )
        )

        assert resp.status_code == 400
        assert _user_counter() == 0
        assert cache.get(TOTAL_KEY) == 1
