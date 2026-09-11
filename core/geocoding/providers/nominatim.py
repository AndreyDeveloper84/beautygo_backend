"""Nominatim (OpenStreetMap) — второй провайдер, свой хост.

Почему второй, а не основной: проба публичного Nominatim на пяти пензенских
адресах (11.09.2026) — **3 из 5 до дома**, «Кирова 20» уехала в Кузнецк,
«Ладожская 130» — только улица. Довод за DaData основным и за ``ambiguous``
как полноправный исход.

Почему всё-таки есть: ODbL разрешает хранить координаты (с атрибуцией
«© OpenStreetMap contributors»), а свой хост снимает и лимит, и зависимость
от чужой оферты. Это единственный провайдер из восьми в исследовании, у
которого право хранения не требует ни письма, ни оплаты.

Политика публичного хоста
-------------------------

``nominatim.openstreetmap.org``: **1 запрос в секунду**, обязательный
``User-Agent``, «no heavy usage». Провайдер соблюдает интервал сам —
это соблюдение политики, а не сокрытие отказа: одиннадцать адресов
занимают одиннадцать секунд, и это напечатано. ``429`` — ``UNAVAILABLE``.
На своём хосте интервал не нужен и выключается настройкой.

Хост — из ``settings.NOMINATIM_BASE_URL``; пусто → ``MISCONFIGURED`` до
первого запроса.
"""
from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from core.geocoding.contract import GeocodeResult, Outcome, Precision

TIMEOUT_SEC = 5
USER_AGENT = "beautygo-catalog geocoder (Ayla, §139)"
PUBLIC_HOST = "nominatim.openstreetmap.org"
PUBLIC_MIN_INTERVAL_SEC = 1.0


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except InvalidOperation:
        return None


def _precision(addr: dict) -> Precision:
    if addr.get("house_number"):
        return Precision.HOUSE
    if addr.get("road"):
        return Precision.STREET
    if addr.get("city") or addr.get("town") or addr.get("village"):
        return Precision.LOCALITY
    return Precision.UNKNOWN


def _locality(addr: dict) -> str:
    return addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""


class NominatimGeocoder:
    name = "nominatim"

    def __init__(self, *, base_url: str | None = None, session=None, sleep=time.sleep):
        self.base_url = (settings.NOMINATIM_BASE_URL if base_url is None else base_url).rstrip("/")
        self._session = session or requests.Session()
        self._sleep = sleep
        self._last_call: float | None = None

    @property
    def is_public_host(self) -> bool:
        return PUBLIC_HOST in self.base_url

    def check(self) -> GeocodeResult | None:
        if not self.base_url:
            return GeocodeResult(
                outcome=Outcome.MISCONFIGURED, provider=self.name,
                reason="NOMINATIM_BASE_URL пуст — свой хост или (для пробы) публичный",
            )
        return None

    def _respect_public_policy(self) -> None:
        if not self.is_public_host:
            return
        if self._last_call is not None:
            wait = PUBLIC_MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = time.monotonic()

    def reverse(self, lat: float, lon: float) -> GeocodeResult:
        params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1}
        self._respect_public_policy()
        try:
            resp = self._session.get(
                f"{self.base_url}/reverse", params=params,
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason=f"сеть: {exc}")
        if resp.status_code == 429 or resp.status_code >= 500:
            return GeocodeResult(
                outcome=Outcome.UNAVAILABLE, provider=self.name,
                reason=f"HTTP {resp.status_code} — лимит или сбой, повторить позже",
            )
        if resp.status_code != 200:
            return GeocodeResult(
                outcome=Outcome.UNAVAILABLE, provider=self.name,
                reason=f"неожиданный HTTP {resp.status_code}",
            )
        try:
            item = resp.json()
        except ValueError:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason="ответ не JSON")
        # /reverse отвечает объектом; «ничего нет» — объект с ключом error.
        if not isinstance(item, dict) or item.get("error"):
            return GeocodeResult(outcome=Outcome.NOT_FOUND, provider=self.name)
        locality = _locality(item.get("address") or {})
        if not locality:
            return GeocodeResult(outcome=Outcome.NOT_FOUND, provider=self.name, reason="в ответе нет города")
        return GeocodeResult(
            outcome=Outcome.FOUND, provider=self.name,
            normalized_address=item.get("display_name") or "", locality=locality,
        )

    def geocode(self, address: str, *, city: str = "") -> GeocodeResult:
        # Город — частью запроса: у Nominatim нет фильтра «в этом городе»,
        # но строка «…, Пенза» ранжирует пензенский дом выше кузнецкого.
        q = address if not city or city.lower() in address.lower() else f"{address}, {city}"
        params = {"q": q, "format": "jsonv2", "addressdetails": 1, "limit": 2}
        self._respect_public_policy()
        try:
            resp = self._session.get(
                f"{self.base_url}/search", params=params,
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason=f"сеть: {exc}")

        if resp.status_code == 429 or resp.status_code >= 500:
            return GeocodeResult(
                outcome=Outcome.UNAVAILABLE, provider=self.name,
                reason=f"HTTP {resp.status_code} — лимит или сбой, повторить позже",
            )
        if resp.status_code != 200:
            return GeocodeResult(
                outcome=Outcome.UNAVAILABLE, provider=self.name,
                reason=f"неожиданный HTTP {resp.status_code}",
            )
        try:
            items = resp.json()
        except ValueError:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason="ответ не JSON")
        if not isinstance(items, list) or not items:
            return GeocodeResult(outcome=Outcome.NOT_FOUND, provider=self.name)

        first = items[0]
        addr = first.get("address") or {}
        result = GeocodeResult(
            outcome=Outcome.FOUND, provider=self.name,
            latitude=_decimal(first.get("lat")), longitude=_decimal(first.get("lon")),
            normalized_address=first.get("display_name") or "",
            precision=_precision(addr),
            provider_precision=str(first.get("addresstype") or first.get("type") or ""),
            locality=_locality(addr),
        )
        if len(items) > 1 and _is_a_twin(addr, items[1].get("address") or {}, first, items[1]):
            return GeocodeResult(
                outcome=Outcome.MULTIPLE, provider=self.name,
                latitude=result.latitude, longitude=result.longitude,
                normalized_address=result.normalized_address, precision=result.precision,
                provider_precision=result.provider_precision, locality=result.locality,
                reason="два дома с одним номером на разных координатах",
            )
        return result


def _is_a_twin(addr_a: dict, addr_b: dict, item_a: dict, item_b: dict) -> bool:
    """То же правило, что у DaData: тот же номер дома, другие координаты."""
    ha, hb = (addr_a.get("house_number") or "").strip().lower(), (addr_b.get("house_number") or "").strip().lower()
    if not ha or ha != hb:
        return False
    return (item_a.get("lat"), item_a.get("lon")) != (item_b.get("lat"), item_b.get("lon"))
