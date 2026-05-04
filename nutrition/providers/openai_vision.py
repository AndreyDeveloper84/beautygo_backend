"""OpenAI Vision provider (gpt-4o multimodal).

Reuses the AI Chat OpenAI client (``ai.services.llm_client.get_openai_client``)
so the OPENAI_PROXY (px6) plumbing already smoke-tested for AI Chat flows
through here too — no duplicate proxy setup, no second API key.

Prompt strategy: ask gpt-4o to emit strict JSON with the recognition
fields. Avoids tool-calling here (one-shot, no follow-up) — JSON parsing
is enough.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

from django.conf import settings

from ai.services.llm_client import get_openai_client
from nutrition.providers.base import (
    FoodScannerProvider,
    LowConfidenceError,
    ProviderTimeout,
    ProviderUnavailable,
    ScanResult,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
Ты — диетолог. По фото определи блюдо и оцени порцию.
Отвечай ТОЛЬКО валидным JSON без markdown-обёртки, по схеме:

{
  "dish_name": "название блюда на русском",
  "confidence": 0.0-1.0,
  "portion_g": число в граммах или null если оценить нельзя,
  "ingredients": ["ингредиент1", "ингредиент2", ...]
}

Если на фото не еда — верни confidence: 0 и dish_name: "не еда".
Если еда но не узнаёшь — confidence ≤ 0.4 и dish_name: предположение.
"""


class OpenAIVisionProvider(FoodScannerProvider):
    name = "openai"

    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        timeout_s: float = 12.0,
        max_tokens: int = 400,
        confidence_threshold: float = 0.4,
    ) -> None:
        self._model = model
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
        caption: str = "",
    ) -> ScanResult:
        if not getattr(settings, "OPENAI_API_KEY", ""):
            raise ProviderUnavailable("OPENAI_API_KEY is not configured")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        image_url = f"data:image/jpeg;base64,{b64}"

        # Caption (DRF-303) is sanitised + truncated before going into the
        # prompt — caller-supplied free text shouldn't unbalance the JSON
        # contract. 280 chars is the conservative ceiling Tweet-style; the
        # bot UX caps inputs at ~200.
        user_text = "Что на фото? Верни JSON по схеме."
        clean_caption = (caption or "").strip()[:280]
        if clean_caption:
            user_text += (
                f"\n\nПодпись пользователя (учти контекст порции/варианта): "
                f"{clean_caption}"
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]

        started = time.monotonic()
        try:
            client = get_openai_client()
            completion = client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=0.2,
                timeout=self._timeout_s,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - started) * 1000
            logger.warning(
                "nutrition.openai_vision.error elapsed_ms=%.0f err=%s",
                elapsed, exc,
            )
            if _is_timeout(exc):
                raise ProviderTimeout(str(exc)) from exc
            raise ProviderUnavailable(str(exc)) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        content = completion.choices[0].message.content or ""

        parsed = _safe_json(content)
        if parsed is None:
            logger.warning(
                "nutrition.openai_vision.parse_failed content=%s", content[:200],
            )
            raise ProviderUnavailable("OpenAI returned non-JSON content")

        result = self._build_result(parsed, latency_ms, portion_multiplier)
        if result.confidence < self.confidence_threshold:
            raise LowConfidenceError(
                f"openai confidence {result.confidence:.2f} < {self.confidence_threshold}",
                partial=result,
            )
        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _build_result(
        self, parsed: dict[str, Any], latency_ms: int, portion_multiplier: float
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
            raw_response=parsed,
        )


# ---------------------------------------------------------------------------
# helpers (module level — easy to monkeypatch in tests)
# ---------------------------------------------------------------------------


def _safe_json(content: str) -> dict[str, Any] | None:
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _is_timeout(exc: BaseException) -> bool:
    """Distinguish OpenAI/httpx timeouts from generic 5xx for the router."""
    name = type(exc).__name__.lower()
    return "timeout" in name or "timedout" in name
