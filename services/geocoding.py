"""Обратное геокодирование через Yandex Geocoder API (DRF-197).

Возвращает название города по (lat, lon). Если `YANDEX_GEOCODER_API_KEY` не
задан или вызов упал — возвращает None, чтобы вызывающий код мог
фоллбэкнуться на `default`.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_YANDEX_URL = "https://geocode-maps.yandex.ru/1.x/"
_TIMEOUT_SEC = 3


def reverse_geocode_city(lat: float, lon: float) -> Optional[str]:
    """Возвращает название города (например, «Пенза») по координатам.

    Silent fallback на None при:
      * отсутствии `settings.YANDEX_GEOCODER_API_KEY`
      * сетевой ошибке / таймауте
      * пустом/некорректном ответе Yandex
    """
    api_key = getattr(settings, 'YANDEX_GEOCODER_API_KEY', '') or ''
    if not api_key:
        return None

    params = {
        'apikey': api_key,
        'geocode': f"{lon},{lat}",
        'format': 'json',
        'kind': 'locality',
        'results': 1,
        'lang': 'ru_RU',
    }
    try:
        response = requests.get(_YANDEX_URL, params=params, timeout=_TIMEOUT_SEC)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Yandex Geocoder request failed: %s", exc)
        return None

    try:
        features = (
            response.json()
            ['response']['GeoObjectCollection']['featureMember']
        )
        if not features:
            return None
        return features[0]['GeoObject']['name']
    except (KeyError, ValueError) as exc:
        logger.warning("Yandex Geocoder malformed response: %s", exc)
        return None
