"""Реестр провайдеров и закрытый список модулей, которые ходят к геокодерам.

Два списка, и оба стережёт тест:

``PROVIDERS`` — что можно выбрать в ``geocode_tenants --provider``. Имя
здесь равно тому, что ляжет в ``Tenant.geocode_provider``.

``GEOCODING_MODULES`` — **все** модули репозитория, в которых есть вызов
внешнего геокодера. Сегодня их два, и второй — не отсюда:
``services/geocoding.py`` (обратное геокодирование, Яндекс, с апреля).
Список закрыт: появление третьего модуля с адресом геокодера — красный
тест, потому что два геокодера без связи уже есть, а три не распутает никто.
"""
from __future__ import annotations

from core.geocoding.providers.base import Geocoder
from core.geocoding.providers.dadata import DaDataGeocoder
from core.geocoding.providers.nominatim import NominatimGeocoder
from core.geocoding.providers.yandex import YandexGeocoder

#: имя → класс. Порядок — рекомендация исследования: DaData основным,
#: Nominatim вторым, Яндекс — заглушка до покупки лицензии.
PROVIDERS: dict[str, type] = {
    DaDataGeocoder.name: DaDataGeocoder,
    NominatimGeocoder.name: NominatimGeocoder,
    YandexGeocoder.name: YandexGeocoder,
}

#: Хосты внешних геокодеров, по которым сторож ищет модули.
GEOCODER_HOST_MARKERS: tuple[str, ...] = (
    "geocode-maps.yandex.ru",
    "dadata.ru",
    "nominatim",
)

#: Закрытый список модулей, которым можно эти хосты упоминать.
GEOCODING_MODULES: frozenset[str] = frozenset({
    "services/geocoding.py",                   # обратный, Яндекс, с 25.04 — не трогается
    "core/geocoding/providers/__init__.py",    # этот реестр
    "core/geocoding/providers/yandex.py",      # заглушка
    "core/geocoding/providers/dadata.py",
    "core/geocoding/providers/nominatim.py",
})


def get_provider(name: str) -> Geocoder:
    try:
        cls = PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS)) or "(пусто)"
        raise ValueError(f"неизвестный провайдер {name!r}; известны: {known}") from None
    return cls()
