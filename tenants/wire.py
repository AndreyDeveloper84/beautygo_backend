"""Провод: адрес и координаты места предложения в ответах API (§9, L6, DRF-1687).

До L6 четыре сериализатора отдавали клиенту ``address`` / ``location_lat`` /
``location_lng`` **профиля мастера** — адрес человека. После L6 имена на
проводе те же (клиенты их читают), но значение берётся из места
(``works_at``), и только когда место подтверждено; координаты — только
когда место ещё и геокодировано (та же ось, что у расстояния).

Три поля вместо ``source='specialist.location_lat'``: подмена источника
в одном классе, а не в четырёх списках. Сторож
``tenants/tests/test_distance_authority_1687.py`` держит список читателей
старых полей закрытым — эти поля в нём не значатся, потому что старые
поля не читают.
"""
from __future__ import annotations

from rest_framework import serializers

from .distance import offer_address, offer_point


class OfferAddressField(serializers.ReadOnlyField):
    """Адрес подтверждённого места, либо ``""``. ``source`` — мастер."""

    def to_representation(self, specialist) -> str:
        return offer_address(specialist)


class _OfferCoordinateField(serializers.DecimalField):
    axis: str

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 9)
        kwargs.setdefault("decimal_places", 6)
        kwargs["read_only"] = True
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)

    def to_representation(self, specialist):
        # ``offer_point`` — тот же критерий участия, что у расстояния:
        # координата на проводе есть ровно тогда, когда есть расстояние.
        if offer_point(specialist) is None:
            return None
        return super().to_representation(getattr(specialist.works_at, self.axis))


class OfferLatitudeField(_OfferCoordinateField):
    axis = "latitude"


class OfferLongitudeField(_OfferCoordinateField):
    axis = "longitude"
