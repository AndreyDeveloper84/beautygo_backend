"""Abstract base + DTOs for food-scanner providers.

Each concrete provider (OpenAI Vision, Yandex Vision, future self-host
ViT) implements ``scan(image_bytes, user)`` and returns a ``ScanResult``.
The router (Slice 2) calls one provider, falls back on certain failures,
and persists the result in ``FoodScan`` (model, also Slice 2).

Failure taxonomy:

- ``ProviderTimeout``    → router fallback (network slowness, gateway 504)
- ``ProviderUnavailable``→ router fallback (5xx, missing API key, upstream down)
- ``LowConfidenceError`` → router fallback (model returned but confidence
                           below threshold — try the other vendor)
- Any other exception    → bubbles up; router will not silently retry to
                           avoid burning $ on a configuration bug

Providers are SIDE-EFFECT-FREE on persistence. They make the network call,
parse, and return. Persisting ``FoodScan`` and emitting analytics happens
one layer up in the router/service.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScanResult:
    """Provider-agnostic result shape.

    Mirrors what the API endpoint will eventually return (per Notion API
    Spec v2.0 §FOOD SCANNER `FoodScanResponse`). Nutrition data is set
    by Slice 3 (USDA / Open Food Facts lookup); providers only fill the
    recognition fields.
    """

    dish_name: str
    confidence: float                # 0.0 - 1.0
    portion_g: float | None          # provider's best guess; None if not estimable
    ingredients: list[str] = field(default_factory=list)
    provider: str = ""               # "openai" / "yandex" / "vit-self-host"
    latency_ms: int = 0
    raw_response: dict[str, Any] = field(default_factory=dict)  # debug/audit


@dataclass(frozen=True)
class ProviderResponse:
    """What the router stores for the FoodScan row.

    A thin wrapper that bundles ``ScanResult`` with the original image
    reference (S3 key) so we don't need to re-upload on retries.
    """

    result: ScanResult
    image_s3_key: str = ""


class ProviderError(Exception):
    """Base for all provider-side errors."""


class ProviderTimeout(ProviderError):
    """Network timeout reaching the provider."""


class ProviderUnavailable(ProviderError):
    """Provider returned 5xx, has no API key, or signalled a service-level
    failure (e.g. 'model overloaded')."""


class LowConfidenceError(ProviderError):
    """Provider returned a result but confidence is below threshold —
    router should try fallback before showing the user 'not recognised'."""

    def __init__(self, message: str, partial: ScanResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class FoodScannerProvider(ABC):
    """Abstract food-scanner provider.

    Subclasses implement ``scan()`` and ``name``. Settings reads happen
    inside ``scan()`` so Django config errors surface as
    ``ProviderUnavailable`` rather than at import time.
    """

    name: str = ""              # e.g. "openai", "yandex", "vit-self-host"
    confidence_threshold: float = 0.4  # below this → LowConfidenceError

    @abstractmethod
    def scan(
        self,
        image_bytes: bytes,
        *,
        portion_multiplier: float = 1.0,
        user=None,
        caption: str = "",
    ) -> ScanResult:
        """Recognise dish in ``image_bytes``.

        Args:
            image_bytes: JPEG/PNG/HEIC bytes. Provider is responsible for
                any format conversion it needs.
            portion_multiplier: Mobile sends this when the user adjusts
                portion size pre-confirm. Provider passes it through to
                ``ScanResult.portion_g`` (multiplied) when applicable.
            user: ``users.models.User`` instance — passed for future
                provider routing per user preferences (Phase 6). Not
                used in MVP.
            caption: Optional free-text user note about the photo (DRF-303).
                Steerable providers (OpenAI Vision) inject it into the
                vision prompt so portion / variant cues («это половина
                порции у мамы») are respected. Non-steerable providers
                (Yandex Vision) must accept the kwarg for interface
                consistency and ignore it.

        Returns:
            ScanResult populated with recognition fields. Nutrition lookup
            is the caller's job.

        Raises:
            ProviderTimeout: network timeout
            ProviderUnavailable: 5xx, missing key, model overloaded
            LowConfidenceError: confidence < ``confidence_threshold``
        """
        ...
