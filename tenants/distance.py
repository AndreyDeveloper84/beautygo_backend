"""Расстояние до места оказания услуги — единственное место, где оно считается (§9, DRF-1687, L5).

Решение владельца §9: «расстояние считается до точки конкретного предложения,
а не до профиля мастера». §8: «отсутствие координат даёт DISTANCE_UNKNOWN, а
не ноль или средний score».

До L5 расстояние считали четыре места, и каждое — до ``SpecialistProfile.
location_lat/lng`` (адрес человека): движок подбора, два места в поиске,
карточка и bbox-фильтр «рядом». Все четыре были безвредны ровно потому, что
координат не было ни у одного из 31 мастера; первый геокодированный включил
бы их разом и посчитал расстояние до человека.

Теперь точка предложения — ``specialist.works_at`` (``ServiceLocation``), и
только если место **подтверждено и геокодировано** (``participates_in_distance``:
две оси в одном свойстве, см. модель). Всё остальное — ``None`` =
``DISTANCE_UNKNOWN``: не 0.5, не 0, не «далеко».

Одна формула, одна проверка, одна выборка для ORM — чтобы четыре читателя не
разъехались снова. Сторож в ``tenants/tests/test_distance_authority_1687.py``
следит, что никто не считает расстояние мимо этого модуля.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING

from django.db.models import Q

from .models import GeocodeStatus
from .service_location import LocationStatus

if TYPE_CHECKING:  # pragma: no cover
    from users.models import SpecialistProfile

EARTH_RADIUS_KM = 6371.0

#: Статусы геокодера, при которых координаты пригодны — как в модели.
_GEOCODED = (GeocodeStatus.OK.value, GeocodeStatus.CONFIRMED.value)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Ортодромия в километрах. Одна на весь каталог."""
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def offer_point(specialist: "SpecialistProfile") -> tuple[float, float] | None:
    """Координаты точки предложения мастера, либо ``None`` = DISTANCE_UNKNOWN.

    Читает ``works_at`` — вызывающий обязан ``select_related("works_at")``,
    иначе N+1. Старые ``location_lat/lng`` профиля здесь не читаются
    намеренно и никогда: они принадлежат человеку (§9).
    """
    place = specialist.works_at
    if place is None or not place.participates_in_distance:
        return None
    return float(place.latitude), float(place.longitude)


def distance_km_to(specialist: "SpecialistProfile", lat: float | None, lon: float | None) -> float | None:
    """Расстояние от точки клиента до места предложения, либо ``None``.

    ``None`` при любой неизвестной стороне: нет координаты клиента (§8 — не
    угадываем, откуда человек поедет) или нет подтверждённого геокодированного
    места. Нулей и середин шкалы здесь нет.
    """
    if lat is None or lon is None:
        return None
    point = offer_point(specialist)
    if point is None:
        return None
    return haversine_km(float(lat), float(lon), *point)


def participating_place_q(prefix: str = "works_at") -> Q:
    """Q для ORM: у мастера есть место, участвующее в расстоянии.

    То же условие, что ``ServiceLocation.participates_in_distance``, но для
    выборки: подтверждено, геокодировано, обе координаты есть и не (0, 0).
    Копия условия неизбежна — свойство в SQL не вызвать, — поэтому тест
    держит обе стороны рядом: строка, которая проходит одно, проходит другое.
    """
    p = prefix
    return (
        Q(**{f"{p}__status": LocationStatus.CONFIRMED})
        & Q(**{f"{p}__geocode_status__in": _GEOCODED})
        & Q(**{f"{p}__latitude__isnull": False})
        & Q(**{f"{p}__longitude__isnull": False})
        & ~(Q(**{f"{p}__latitude": 0}) & Q(**{f"{p}__longitude": 0}))
    )


def bbox_q(lat: float, lon: float, radius_km: float, prefix: str = "works_at") -> Q:
    """Грубый прямоугольник вокруг точки — быстрый предфильтр «рядом».

    Только по участвующим местам: мастер без подтверждённого места не
    «далеко», он «неизвестно где», и в радиус не попадает по построению.
    """
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * max(cos(radians(lat)), 1e-6))
    p = prefix
    return participating_place_q(prefix) & Q(**{
        f"{p}__latitude__gte": lat - lat_delta,
        f"{p}__latitude__lte": lat + lat_delta,
        f"{p}__longitude__gte": lon - lon_delta,
        f"{p}__longitude__lte": lon + lon_delta,
    })
