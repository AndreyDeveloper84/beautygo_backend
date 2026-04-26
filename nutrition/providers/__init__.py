"""Food scanner provider implementations.

Slice 1 of docs/FOOD_SCANNER_DECISION.md (Plan Y+ multi-vendor router).

Currently shipped:
- OpenAIVisionProvider — gpt-4o through the existing OPENAI_PROXY plumbing
- YandexVisionProvider — Yandex Cloud Foundation Models (RU jurisdiction)

Slice 2 will add the FoodScannerRouter that picks primary/fallback per
settings and the POST /api/v1/nutrition/scan/ endpoint.
"""
from nutrition.providers.base import (
    FoodScannerProvider,
    LowConfidenceError,
    ProviderResponse,
    ProviderTimeout,
    ProviderUnavailable,
    ScanResult,
)

__all__ = [
    "FoodScannerProvider",
    "ProviderResponse",
    "ScanResult",
    "ProviderTimeout",
    "ProviderUnavailable",
    "LowConfidenceError",
]
