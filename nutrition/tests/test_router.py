"""Unit tests for FoodScannerRouter."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nutrition.providers.base import (
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
    ScanResult,
)
from nutrition.services.food_scanner_router import (
    AllProvidersFailedError,
    FoodScannerRouter,
)


def _scan_result(provider="openai", confidence=0.9):
    return ScanResult(
        dish_name="Борщ",
        confidence=confidence,
        portion_g=300,
        ingredients=["свёкла"],
        provider=provider,
        latency_ms=200,
    )


class _FakeProvider:
    """Test stub that produces a scripted result/exception."""

    def __init__(self, *, return_value=None, side_effect=None):
        self.return_value = return_value
        self.side_effect = side_effect
        self.scan_calls = 0

    def scan(self, image_bytes, *, portion_multiplier=1.0, user=None, caption=""):
        self.scan_calls += 1
        if self.side_effect is not None:
            raise self.side_effect
        return self.return_value


def _registry(primary_provider, fallback_provider=None):
    """Build a registry whose constructors return the given fakes."""
    reg = {"primary": MagicMock(return_value=primary_provider)}
    if fallback_provider is not None:
        reg["fallback"] = MagicMock(return_value=fallback_provider)
    return reg


class TestPrimarySuccess:
    def test_returns_primary_result_no_fallback_attempt(self):
        primary = _FakeProvider(return_value=_scan_result("openai"))
        fallback = _FakeProvider(return_value=_scan_result("yandex"))
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="fallback",
            registry=_registry(primary, fallback),
        )
        outcome = router.scan(b"\xff\xd8\xff")
        assert outcome.result.provider == "openai"
        assert outcome.primary_failed_with == ""
        assert primary.scan_calls == 1
        assert fallback.scan_calls == 0


class TestFallbackOnPrimaryFailure:
    @pytest.mark.parametrize("primary_err", [
        ProviderTimeout("timeout"),
        ProviderUnavailable("503"),
        LowConfidenceError("low", partial=_scan_result("openai", 0.2)),
    ])
    def test_falls_back_on_known_failures(self, primary_err):
        primary = _FakeProvider(side_effect=primary_err)
        fallback = _FakeProvider(return_value=_scan_result("yandex"))
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="fallback",
            registry=_registry(primary, fallback),
        )
        outcome = router.scan(b"\xff\xd8\xff")
        assert outcome.result.provider == "yandex"
        assert outcome.primary_failed_with == type(primary_err).__name__
        assert primary.scan_calls == 1
        assert fallback.scan_calls == 1

    def test_does_not_fallback_on_unknown_exception(self):
        primary = _FakeProvider(side_effect=RuntimeError("config bug"))
        fallback = _FakeProvider(return_value=_scan_result("yandex"))
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="fallback",
            registry=_registry(primary, fallback),
        )
        with pytest.raises(RuntimeError):
            router.scan(b"\xff\xd8\xff")
        assert fallback.scan_calls == 0  # we did NOT silently retry


class TestBothFail:
    def test_both_unavailable_raises_all_failed(self):
        primary = _FakeProvider(side_effect=ProviderUnavailable("openai down"))
        fallback = _FakeProvider(side_effect=ProviderUnavailable("yandex down"))
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="fallback",
            registry=_registry(primary, fallback),
        )
        with pytest.raises(AllProvidersFailedError) as exc_info:
            router.scan(b"\xff\xd8\xff")
        assert "openai down" in str(exc_info.value.primary_err)
        assert "yandex down" in str(exc_info.value.fallback_err)


class TestMisconfiguration:
    def test_no_fallback_configured_raises_after_primary_fail(self):
        primary = _FakeProvider(side_effect=ProviderTimeout("nope"))
        registry = _registry(primary)
        # No "fallback" entry — settings empty
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="",
            registry=registry,
        )
        with pytest.raises(AllProvidersFailedError) as exc_info:
            router.scan(b"\xff\xd8\xff")
        assert exc_info.value.fallback_err is None

    def test_unknown_provider_name_raises(self):
        router = FoodScannerRouter(
            primary_name="bogus",
            fallback_name="fallback",
            registry={},
        )
        # ProviderUnavailable is a fallback-able failure inside the
        # router pipeline → wrapped in AllProvidersFailedError.
        with pytest.raises(AllProvidersFailedError):
            router.scan(b"\xff\xd8\xff")

    def test_fallback_same_as_primary_does_not_double_call(self):
        primary = _FakeProvider(side_effect=ProviderTimeout("nope"))
        registry = _registry(primary)
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="primary",  # same — guard against self-fallback
            registry=registry,
        )
        with pytest.raises(AllProvidersFailedError):
            router.scan(b"\xff\xd8\xff")
        assert primary.scan_calls == 1


class TestPropagation:
    def test_portion_multiplier_passed_to_provider(self):
        primary = _FakeProvider(return_value=_scan_result())
        router = FoodScannerRouter(
            primary_name="primary",
            fallback_name="fallback",
            registry=_registry(primary),
        )
        scan_called_with = {}
        original_scan = primary.scan

        def capture(*args, **kwargs):
            scan_called_with.update(kwargs)
            scan_called_with["args"] = args
            return original_scan(*args, **kwargs)

        primary.scan = capture
        router.scan(b"\xff\xd8\xff", portion_multiplier=2.5)
        assert scan_called_with["portion_multiplier"] == 2.5
