"""Yandex Cloud Foundation Models — Vision provider.

Uses the public Foundation Models API directly via httpx (no SDK — Yandex
publishes the wire format and adding yandex-cloud-ml-sdk pulls in heavy
gRPC deps we don't need for one POST per scan).

Endpoint: ``https://llm.api.cloud.yandex.net/foundationModels/v1/completion``
Auth:     ``Authorization: Api-Key <YANDEX_VISION_API_KEY>``
Model:    ``gpt://<folder_id>/yandexgpt-vision/latest`` (or whatever URI
          Yandex assigns when you enable Vision in your folder).

Yandex servers live in RF — chosen as fallback so we have a 152-ФЗ-clean
path without rebuilding the whole pipeline. RU food coverage is generally
better than the OpenAI default since training data leans that way.

NOTE: exact request/response shape may shift as Yandex iterates. The
fields touched here (model_uri, messages with image content, response
text) are documented at https://yandex.cloud/en/docs/foundation-models/.
Verify with a smoke test before relying in production.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx
from django.conf import settings

from nutrition.providers.base import (
    FoodScannerProvider,
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
    ScanResult,
)
from nutrition.providers.openai_vision import SYSTEM_PROMPT, _safe_json

logger = logging.getLogger(__name__)


YANDEX_API_URL = (
    "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
)


class YandexVisionProvider(FoodScannerProvider):
    name = "yandex"

    def __init__(
        self,
        *,
        timeout_s: float = 12.0,
        max_tokens: int = 400,
        confidence_threshold: float = 0.4,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self.confidence_threshold = confidence_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scan(
        self,
        image_bytes: bytes,
        *,
        portion_multiplier: float = 1.0,
        user=None,
    ) -> ScanResult:
        api_key = getattr(settings, "YANDEX_VISION_API_KEY", "")
        folder_id = getattr(settings, "YANDEX_VISION_FOLDER_ID", "")
        if not api_key or not folder_id:
            raise ProviderUnavailable(
                "YANDEX_VISION_API_KEY / YANDEX_VISION_FOLDER_ID not configured"
            )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = self._build_payload(folder_id, b64)

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(
                    YANDEX_API_URL,
                    headers={
                        "Authorization": f"Api-Key {api_key}",
                        "Content-Type": "application/json",
                        "x-folder-id": folder_id,
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            elapsed = (time.monotonic() - started) * 1000
            logger.warning("nutrition.yandex.timeout elapsed_ms=%.0f", elapsed)
            raise ProviderTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000
            logger.warning(
                "nutrition.yandex.http_error elapsed_ms=%.0f err=%s",
                elapsed, exc,
            )
            raise ProviderUnavailable(str(exc)) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"yandex 5xx: {response.status_code} {response.text[:200]}"
            )
        if response.status_code >= 400:
            # 4xx is usually a config bug (bad key / wrong folder) —
            # bubble up rather than silently fallback.
            raise ProviderUnavailable(
                f"yandex {response.status_code}: {response.text[:200]}"
            )

        body = response.json()
        text = self._extract_text(body)
        parsed = _safe_json(text)
        if parsed is None:
            logger.warning("nutrition.yandex.parse_failed text=%s", text[:200])
            raise ProviderUnavailable("yandex returned non-JSON content")

        result = self._build_result(parsed, latency_ms, portion_multiplier, body)
        if result.confidence < self.confidence_threshold:
            raise LowConfidenceError(
                f"yandex confidence {result.confidence:.2f} < {self.confidence_threshold}",
                partial=result,
            )
        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _build_payload(self, folder_id: str, image_b64: str) -> dict[str, Any]:
        return {
            "modelUri": f"gpt://{folder_id}/yandexgpt-vision/latest",
            "completionOptions": {
                "stream": False,
                "temperature": 0.2,
                "maxTokens": str(self._max_tokens),
            },
            "messages": [
                {"role": "system", "text": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": "Что на фото? Верни JSON по схеме.",
                    "image": image_b64,  # field name per Yandex docs; verify
                },
            ],
        }

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> str:
        """Yandex returns
        {"result": {"alternatives": [{"message": {"text": "..."}}]}}
        Defensive in case the structure shifts.
        """
        try:
            alternatives = (body.get("result") or {}).get("alternatives") or []
            if not alternatives:
                return ""
            return (alternatives[0].get("message") or {}).get("text", "")
        except (AttributeError, TypeError, IndexError):
            return ""

    def _build_result(
        self,
        parsed: dict[str, Any],
        latency_ms: int,
        portion_multiplier: float,
        raw_body: dict[str, Any],
    ) -> ScanResult:
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        portion_g_raw = parsed.get("portion_g")
        try:
            portion_g = (
                float(portion_g_raw) * portion_multiplier
                if portion_g_raw is not None
                else None
            )
        except (TypeError, ValueError):
            portion_g = None

        ingredients_raw = parsed.get("ingredients") or []
        ingredients = [
            str(i).strip() for i in ingredients_raw if str(i).strip()
        ][:20]

        return ScanResult(
            dish_name=str(parsed.get("dish_name") or "").strip()[:200],
            confidence=confidence,
            portion_g=portion_g,
            ingredients=ingredients,
            provider=self.name,
            latency_ms=latency_ms,
            raw_response={"parsed": parsed, "yandex_body": raw_body},
        )
