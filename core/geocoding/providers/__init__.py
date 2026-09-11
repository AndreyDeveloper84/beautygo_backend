"""Реестр провайдеров и закрытый список модулей, которые ходят к геокодерам.

Два списка, и оба стережёт тест:

``PROVIDERS`` — что можно выбрать в ``geocode_tenants --provider``. Имя
здесь равно тому, что ляжет в ``Tenant.geocode_provider``.

``GEOCODING_MODULES`` — **все** модули репозитория, в которых есть вызов
внешнего геокодера. С DRF-1685 это только провайдеры этого пакета:
``services/geocoding.py`` (обратное геокодирование для региональной цены)
больше не ходит к Яндексу сам, а берёт провайдера отсюда. Список закрыт:
появление модуля с адресом геокодера вне пакета — красный тест, потому что
в апреле–сентябре в каталоге жили два геокодера без связи, и второй был
мёртв три месяца незаметно.
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
