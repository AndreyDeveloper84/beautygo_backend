"""Обратное геокодирование для региональной цены — через адаптер §139 (DRF-1685).

История, ради которой этот файл переписан
-----------------------------------------

С 25.04.2026 здесь жил прямой вызов Яндекс Геокодера с
``getattr(settings, 'YANDEX_GEOCODER_API_KEY', '')``. Ключ был объявлен
**только в тестах** — в бою всегда пусто, функция всегда ``None``,
региональная цена всегда ``default``, тесты зелёные. Механизм построен и
выключен, и ни одна строка этого не говорила.

Теперь один модуль геокодирования на каталог: провайдер берётся из
``core.geocoding`` по ``settings.GEOCODING_PROVIDER``. Ключ читается из
**объявленной** настройки (``base.py``), не из ``getattr``; отсутствие
ключа — не тихий ``None``, а состояние, которое (а) пишется в лог один раз
на процесс и (б) поднимается системной проверкой ``geocoding.W001/E001``.

Контракт для ``services/pricing.py`` сохранён: функция не бросает
исключений; всё, что не «нашли город», — ``None``, и цена падает на
``default``.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from core.geocoding.contract import Outcome
from core.geocoding.providers import PROVIDERS, get_provider

logger = logging.getLogger(__name__)

_warned_once = False


def reverse_geocoding_readiness() -> Optional[str]:
    """``None`` — провайдер настроен; иначе причина, почему обратное
    геокодирование не работает. Читается системной проверкой и тестами."""
    name = settings.GEOCODING_PROVIDER
    if name not in PROVIDERS:
        return f"GEOCODING_PROVIDER={name!r} не в реестре ({', '.join(sorted(PROVIDERS))})"
    refusal = get_provider(name).check()
    return None if refusal is None else refusal.reason


def reverse_geocode_city(lat: float, lon: float) -> Optional[str]:
    """Название города по координатам, либо ``None``.

    ``None`` при любом исходе, кроме «нашли город»: не настроен, недоступен,
    не нашли. Различие между ними — в логе и в системной проверке, не в
    возвращаемом значении: ``pricing`` нужен город или его отсутствие.
    """
    global _warned_once
    reason = reverse_geocoding_readiness()
    if reason is not None:
        if not _warned_once:
            logger.warning(
                "reverse geocoding выключено: %s — региональная цена по координатам всегда default",
                reason,
            )
            _warned_once = True
        return None

    result = get_provider(settings.GEOCODING_PROVIDER).reverse(float(lat), float(lon))
    if result.outcome is Outcome.FOUND and result.locality:
        return result.locality
    if result.outcome in (Outcome.UNAVAILABLE, Outcome.MISCONFIGURED):
        logger.warning("reverse geocoding: %s (%s)", result.reason, result.provider)
    return None
