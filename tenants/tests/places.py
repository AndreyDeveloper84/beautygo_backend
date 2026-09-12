"""Помощник тестов: поставить мастера в подтверждённое геокодированное место.

§9 / L5: расстояние считается до ``works_at``, а не до ``location_lat/lng``
профиля. Тесты, которым нужен мастер «в такой-то точке», зовут это — и не
трогают старые поля профиля, которые расстояние больше не читает.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation


def place_specialist_at(specialist, lat, lng, *, city: str = "Пенза", label: str = "") -> ServiceLocation:
    """Создать подтверждённое геокодированное место и назначить его мастеру."""
    place = ServiceLocation.objects.create(
        tenant=specialist.tenant, label=label, city=city,
        address=label or f"точка {lat}, {lng}",
        latitude=Decimal(str(lat)), longitude=Decimal(str(lng)),
        geocode_status=GeocodeStatus.OK, geocode_provider="test",
        status=LocationStatus.CONFIRMED, confirmed_by=specialist.user,
        confirmed_at=timezone.now(), confirmed_source_ref="test",
    )
    specialist.works_at = place
    specialist.save(update_fields=["works_at"])
    return place
