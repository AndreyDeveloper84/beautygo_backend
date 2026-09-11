"""Что обязан уметь провайдер: один вызов, один ответ, никаких исключений наружу.

Провайдер **не поднимает** исключений по сетевым причинам — он возвращает
``Outcome.UNAVAILABLE`` с причиной. Иначе один таймаут ронял бы прогон на
одиннадцатом адресе, и десять предыдущих исходов пришлось бы восстанавливать
из логов. Исключение — ``MISCONFIGURED``: его провайдер обязан вернуть
**до** первого сетевого вызова, из ``check()``, чтобы прогон остановился,
не потратив ни одного запроса из дневного лимита.
"""
from __future__ import annotations

from typing import Protocol

from core.geocoding.contract import GeocodeResult


class Geocoder(Protocol):
    #: Имя, которое ложится в ``Tenant.geocode_provider``. §139: провайдер в
    #: записи делает замену провайдера наблюдаемой.
    name: str

    def check(self) -> GeocodeResult | None:
        """``None`` — настроен; иначе результат с ``MISCONFIGURED`` и причиной."""
        ...

    def geocode(self, address: str) -> GeocodeResult:
        """Один адрес → один ответ. ``count=1``: см. п. 4.2.1 оферты DaData."""
        ...
