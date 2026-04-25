"""Определение региона для рекомендации цен (DRF-197).

Единственная публичная функция — `get_region_key()`. Маппинг добавлять
здесь же: новый регион = запись в `CITY_TO_REGION_KEY`.
"""
from __future__ import annotations

from typing import Optional

from .geocoding import reverse_geocode_city
from .models import RegionalPricing

# Нормализованный city-name (lowercase, без диакритики) → region_key.
# Расширяй по мере запуска в новых городах.
CITY_TO_REGION_KEY: dict[str, str] = {
    'пенза': 'penza',
    'penza': 'penza',
}


def _normalize(value: Optional[str]) -> str:
    if not value:
        return ''
    return value.strip().lower()


def _map_city_to_region(city: Optional[str]) -> Optional[str]:
    key = _normalize(city)
    if not key:
        return None
    return CITY_TO_REGION_KEY.get(key)


def get_region_key(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    city_input: Optional[str] = None,
) -> str:
    """Определяет region_key по приоритету: city_input → geocoding → default.

    * `city_input` — явный выбор города мастером (например, из dropdown).
    * `lat`/`lon` — GPS устройства; используется reverse-geocoding.
    * Если ничего не распознано — возвращается `RegionalPricing.DEFAULT_REGION_KEY`.

    Функция не бросает исключений: сбой geocoding → default.
    """
    region = _map_city_to_region(city_input)
    if region is not None:
        return region

    if lat is not None and lon is not None:
        try:
            city = reverse_geocode_city(float(lat), float(lon))
        except (TypeError, ValueError):
            city = None
        region = _map_city_to_region(city)
        if region is not None:
            return region

    return RegionalPricing.DEFAULT_REGION_KEY
