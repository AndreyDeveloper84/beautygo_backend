"""Путь записи: результат провайдера → восемь полей ``ServiceLocation``.

§9 (DRF-1687, L4): цель записи адаптера — **место оказания услуги**, не салон.
``Tenant.address`` остаётся входом (перенос — ``promote_tenant_location``),
восемь полей провенанса на ``Tenant`` доживают до нуля читателей (L8).

Три правила, каждое — про то, чего адаптер НЕ делает:

* **человека не перезаписывает.** ``CONFIRMED`` — исход человека, и никакой
  ответ сервиса его не отменяет. Строка пропускается с названной причиной;
* **хорошее не портит недоступностью.** ``UNAVAILABLE`` — это «новостей
  нет», а не «новости плохие». Уже геокодированная строка от лежащего
  сервиса не становится ``pending``; иначе один сбой снимал бы с карты
  весь город;
* **фикцию не записывает.** Пара (0, 0) и половина пары не проходят
  ``full_clean()`` — это правило модели из ``#333``, и здесь оно не
  обходится через ``update()``. Сторож проверяет, что путь записи идёт
  через ``full_clean``, а не мимо.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from core.geocoding.contract import GeocodeResult, Outcome, status_for
from tenants.models import GeocodeStatus, ServiceLocation

#: Поля, которые пишет адаптер. Ровно восемь из ``#333`` — и ни одного
#: сверх: ``address`` и ``city`` адаптер читает, но не трогает.
WRITTEN_FIELDS: tuple[str, ...] = (
    "geocode_source_address",
    "geocode_normalized_address",
    "latitude",
    "longitude",
    "geocode_provider",
    "geocode_precision",
    "geocode_status",
    "geocoded_at",
)


def _key(location: ServiceLocation) -> str:
    """Чем строка называется в отчёте: slug салона и метка/адрес места."""
    head = location.label or location.address
    return f"{location.tenant.slug}/{head}" if location.tenant_id else f"соло/{head}"


@dataclass(frozen=True)
class Applied:
    """Что произошло со строкой. ``status`` пуст, если запись пропущена."""

    key: str  #: slug салона / место — или «соло / место»
    status: GeocodeStatus | None
    skipped_because: str = ""

    @property
    def written(self) -> bool:
        return self.status is not None


def apply_result(
    location: ServiceLocation,
    result: GeocodeResult,
    *,
    source_address: str,
    now: datetime | None = None,
    overwrite_ok: bool = False,
    dry_run: bool = False,
) -> Applied:
    """Записать исход в строку — или назвать, почему нет.

    ``dry_run`` считает всё то же самое и **не сохраняет**: команда обязана
    напечатать, что она сделает, до того, как сделает. Возвращаемое значение
    при сухом прогоне такое же, как при настоящем, — иначе сухой прогон
    показывал бы не то, что случится. Объект в памяти при этом изменён
    (через него прошёл ``full_clean``), база — нет; повторно его не
    сохранять.
    """
    if result.outcome is Outcome.MISCONFIGURED:
        raise ValueError(
            "MISCONFIGURED до записи не доходит: провайдер обязан отказать в check(), "
            "а команда — остановиться. " + result.reason
        )

    current = location.geocode_status
    if current == GeocodeStatus.CONFIRMED:
        return Applied(_key(location), None, "подтверждено человеком — адаптер не перезаписывает")
    if current == GeocodeStatus.OK and not overwrite_ok:
        return Applied(_key(location), None, "уже геокодировано — без --overwrite-ok не трогается")
    if result.outcome is Outcome.UNAVAILABLE and location.is_geocoded:
        return Applied(_key(location), None, "сервис недоступен — геокодированную строку не портим")

    status = status_for(result, expected_city=location.city)
    now = now or timezone.now()

    location.geocode_source_address = source_address
    location.geocode_normalized_address = result.normalized_address
    location.latitude = result.latitude
    location.longitude = result.longitude
    location.geocode_provider = result.provider
    location.geocode_precision = result.provider_precision
    location.geocode_status = status
    location.geocoded_at = now

    # Правила модели — единственный сторож фикции, и он обязан сработать
    # здесь, а не остаться в admin-форме. Ошибка поднимается наверх: строка
    # с (0,0) от провайдера — это дефект провайдера, и молча превращать её в
    # pending значило бы спрятать его.
    location.full_clean(validate_unique=False)

    if not dry_run:
        location.save(update_fields=WRITTEN_FIELDS)
    return Applied(_key(location), status)
