"""FoodScannerRouter — primary/fallback orchestration for /nutrition/scan/.

Picks provider by ``settings.FOOD_SCANNER_PRIMARY`` / ``FOOD_SCANNER_FALLBACK``.
On primary failure that we classify as "transient or vendor-specific"
(``ProviderTimeout`` / ``ProviderUnavailable`` / ``LowConfidenceError``)
the router tries fallback. Configuration bugs and unexpected exceptions
bubble up so we don't burn $ retrying the same broken call against
the second vendor.

Self-host ViT (Phase 6) plugs in here as a third provider name without
touching the endpoint or the model — same FoodScannerProvider interface.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Type

from django.conf import settings

from nutrition.providers.base import (
    FoodScannerProvider,
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
    ScanResult,
)
from nutrition.providers.openai_vision import OpenAIVisionProvider
from nutrition.providers.yandex import YandexVisionProvider

logger = logging.getLogger(__name__)


PROVIDER_REGISTRY: dict[str, Type[FoodScannerProvider]] = {
    "openai": OpenAIVisionProvider,
    "yandex": YandexVisionProvider,
}


@dataclass(frozen=True)
class RouterResult:
    """What the router hands back to the view layer.

    ``primary_failed_with`` is set when fallback was used — useful for
    logging and the cost dashboard.
    """

    result: ScanResult
    primary_failed_with: str = ""           # error class name if fallback used
    primary_provider_name: str = ""          # what we tried first


class AllProvidersFailedError(Exception):
    """Both primary and fallback failed.

    ``is_low_confidence_only`` lets the view distinguish two failure modes
    per Notion API Spec v2.0 §FOOD SCANNER:

    - All providers returned a result but with confidence below threshold
      (vendor worked, image just isn't recognisable) → view returns
      400 ``FOOD_NOT_RECOGNIZED``
    - Any provider failed with timeout / unavailable / config error
      (vendor down or unreachable) → view returns 503 ``FOOD_API_UNAVAILABLE``

    Mixed cases (one low-confidence + one timeout) classify as 503 — we
    don't know whether retrying primary would have succeeded.
    """

    def __init__(self, primary_err: Exception, fallback_err: Exception | None) -> None:
        self.primary_err = primary_err
        self.fallback_err = fallback_err
        super().__init__(
            f"primary={type(primary_err).__name__}({primary_err}) "
            f"fallback={type(fallback_err).__name__ if fallback_err else 'not_attempted'}"
        )

    @property
    def is_low_confidence_only(self) -> bool:
        """All attempted providers returned a low-confidence result."""
        if not isinstance(self.primary_err, LowConfidenceError):
            return False
        if self.fallback_err is None:
            return True
        return isinstance(self.fallback_err, LowConfidenceError)


class FoodScannerRouter:
    """Orchestrates primary → fallback provider attempts."""

    # Errors that warrant trying the other vendor. Anything else
    # (ImportError, AttributeError, programming bug) bubbles up.
    FALLBACKABLE = (ProviderTimeout, ProviderUnavailable, LowConfidenceError)

    def __init__(
        self,
        *,
        primary_name: str | None = None,
        fallback_name: str | None = None,
        registry: dict[str, Type[FoodScannerProvider]] | None = None,
    ) -> None:
        # `None` means "use settings"; "" means "explicitly no fallback"
        # so tests can disable fallback without monkey-patching settings.
        self._primary_name = (
            primary_name
            if primary_name is not None
            else settings.FOOD_SCANNER_PRIMARY
        )
        self._fallback_name = (
            fallback_name
            if fallback_name is not None
            else settings.FOOD_SCANNER_FALLBACK
        )
        self._registry = registry or PROVIDER_REGISTRY

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scan(
        self,
        image_bytes: bytes,
        *,
        portion_multiplier: float = 1.0,
        user=None,
        caption: str = "",
    ) -> RouterResult:
        # Capture primary failure into a function-scoped name. The `as`
        # binding inside `except` is cleared at end of the block in
        # Python 3 — without this rebind we'd hit UnboundLocalError below.
        primary_err: Exception | None = None

        try:
            primary = self._build_provider(self._primary_name)
            result = primary.scan(
                image_bytes,
                portion_multiplier=portion_multiplier,
                user=user,
                caption=caption,
            )
            return RouterResult(
                result=result,
                primary_provider_name=self._primary_name,
            )
        except self.FALLBACKABLE as exc:
            primary_err = exc
            logger.info(
                "food_scanner.fallback primary=%s err=%s",
                self._primary_name, type(exc).__name__,
            )

        assert primary_err is not None  # narrowing for the type checker

        # Primary failed in a fallback-able way. Try the secondary.
        if not self._fallback_name or self._fallback_name == self._primary_name:
            raise AllProvidersFailedError(primary_err, None)

        try:
            fallback = self._build_provider(self._fallback_name)
            result = fallback.scan(
                image_bytes,
                portion_multiplier=portion_multiplier,
                user=user,
                caption=caption,
            )
            return RouterResult(
                result=result,
                primary_provider_name=self._primary_name,
                primary_failed_with=type(primary_err).__name__,
            )
        except self.FALLBACKABLE as fallback_err:
            logger.warning(
                "food_scanner.both_failed primary=%s fallback=%s",
                type(primary_err).__name__, type(fallback_err).__name__,
            )
            raise AllProvidersFailedError(primary_err, fallback_err) from fallback_err

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _build_provider(self, name: str) -> FoodScannerProvider:
        try:
            cls = self._registry[name]
        except KeyError as exc:
            raise ProviderUnavailable(
                f"Provider '{name}' not registered (registry: "
                f"{list(self._registry.keys())})"
            ) from exc
        return cls()
