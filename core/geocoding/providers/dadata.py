"""DaData «Подсказки» — основной провайдер (§139, амендмент владельца 11.09.2026).

Почему DaData, а не Яндекс из буквы §139: бесплатный тариф Яндекса
запрещает хранить координаты, платный — 226 200 ₽/год; владелец: «карты
очень дорого, поищи бесплатные». DaData: 0 ₽, 10 000 запросов/сутки, отдаёт
``qc_geo`` — готовое поле «точность» из §139.

Что здесь про лицензию, и почему это в коде, а не в README
---------------------------------------------------------

Оферта DaData, **п. 4.2.1**: «запрещается использовать сервис „Подсказки"
для автоматической обработки адресов». Легальный паттерн из их документации:
человек ввёл адрес → **один** запрос → координаты и ``qc_geo``. Отсюда:

* один запрос на адрес (``count=2`` — это ОДИН запрос; второй кандидат
  нужен только чтобы заметить неоднозначность, см. ниже);
* потолок числа адресов за прогон — в команде (``BATCH_CEILING``), не здесь:
  провайдер не знает, сколько ещё будет;
* право хранить координаты у себя оферта **не запрещает явно** (прочитана
  постранично, ``RESEARCH_MAPS_DISTANCE_2026-09-11.md`` §8); письмо в
  поддержку — отдельная строка, потому что п. 2.3 позволяет менять оферту
  публикацией.

Неоднозначность без флага от провайдера
---------------------------------------

«Подсказки» не говорят «адрес неоднозначен» — они ранжируют. Правило здесь
узкое и названное: **два кандидата домовой точности с ОДНИМ И ТЕМ ЖЕ
номером дома и разными координатами** — ``MULTIPLE``. «д 20» и «д 20А» —
не неоднозначность, первый и есть ответ. Два разных «д 20» на одной улице
(дублирующиеся ФИАС-записи, новостройки) — неоднозначность, и §139 велит
отдать её человеку.

Город
-----

Запрос сужается фильтром ``locations: [{"city": …}]`` — без него «Кирова
20» уходит в другой город с домовой точностью (проба 11.09). Проверка
«там ли нашли» всё равно делается контрактом по ответу.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from core.geocoding.contract import GeocodeResult, Outcome, Precision

SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
TIMEOUT_SEC = 5

#: ``qc_geo`` DaData → общая точность. Словарь провайдера хранится рядом
#: как есть (``provider_precision``), это — только для порога контракта.
#:   0 точные координаты дома · 1 ближайший дом · 2 улица ·
#:   3 населённый пункт · 4 город · 5 не определены
QC_GEO_TO_PRECISION: dict[str, Precision] = {
    "0": Precision.HOUSE,
    "1": Precision.HOUSE,
    "2": Precision.STREET,
    "3": Precision.LOCALITY,
    "4": Precision.LOCALITY,
    "5": Precision.UNKNOWN,
}


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _locality(data: dict) -> str:
    # Город либо населённый пункт внутри района; ``city_with_type`` даёт
    # «г Пенза» — контракт сравнивает по вхождению, поэтому годится любое.
    return data.get("city") or data.get("settlement") or data.get("city_with_type") or ""


class DaDataGeocoder:
    name = "dadata"

    def __init__(self, *, api_key: str | None = None, session=None):
        # Ключ — из settings.DADATA_API_KEY, объявленного в base.py. Не
        # getattr(settings, ..., ""): отсутствие строки в настройках —
        # ошибка кода, а не «пусто по решению» (DRF-1685).
        self.api_key = settings.DADATA_API_KEY if api_key is None else api_key
        self._session = session or requests.Session()

    def check(self) -> GeocodeResult | None:
        if not self.api_key:
            return GeocodeResult(
                outcome=Outcome.MISCONFIGURED, provider=self.name,
                reason="DADATA_API_KEY пуст — задать в окружении; ключ у владельца",
            )
        return None

    def geocode(self, address: str, *, city: str = "") -> GeocodeResult:
        payload: dict = {"query": address, "count": 2}
        if city:
            payload["locations"] = [{"city": city}]
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(SUGGEST_URL, json=payload, headers=headers, timeout=TIMEOUT_SEC)
        except requests.RequestException as exc:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason=f"сеть: {exc}")

        if resp.status_code in (401, 403):
            # Ключ отклонён — это про нас, не про сервис; повтор не поможет.
            return GeocodeResult(
                outcome=Outcome.MISCONFIGURED, provider=self.name,
                reason=f"DaData отклонила ключ (HTTP {resp.status_code})",
            )
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
            suggestions = resp.json().get("suggestions") or []
        except ValueError:
            return GeocodeResult(outcome=Outcome.UNAVAILABLE, provider=self.name, reason="ответ не JSON")

        if not suggestions:
            return GeocodeResult(outcome=Outcome.NOT_FOUND, provider=self.name)

        first = suggestions[0]
        data = first.get("data") or {}
        qc_geo = str(data.get("qc_geo") if data.get("qc_geo") is not None else "")
        result = GeocodeResult(
            outcome=Outcome.FOUND, provider=self.name,
            latitude=_decimal(data.get("geo_lat")), longitude=_decimal(data.get("geo_lon")),
            normalized_address=first.get("unrestricted_value") or first.get("value") or "",
            precision=QC_GEO_TO_PRECISION.get(qc_geo, Precision.UNKNOWN),
            provider_precision=qc_geo, locality=_locality(data),
        )

        if len(suggestions) > 1 and _is_a_twin(data, suggestions[1].get("data") or {}):
            return GeocodeResult(
                outcome=Outcome.MULTIPLE, provider=self.name,
                latitude=result.latitude, longitude=result.longitude,
                normalized_address=result.normalized_address, precision=result.precision,
                provider_precision=qc_geo, locality=result.locality,
                reason="два кандидата домовой точности с одним номером дома",
            )
        return result


def _is_a_twin(a: dict, b: dict) -> bool:
    """Второй кандидат — тот же дом в другом месте, а не соседний дом."""
    house_a, house_b = (a.get("house") or "").strip().lower(), (b.get("house") or "").strip().lower()
    if not house_a or house_a != house_b:
        return False
    if str(a.get("qc_geo")) not in ("0", "1") or str(b.get("qc_geo")) not in ("0", "1"):
        return False
    return (a.get("geo_lat"), a.get("geo_lon")) != (b.get("geo_lat"), b.get("geo_lon"))
