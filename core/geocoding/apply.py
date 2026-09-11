"""Путь записи: результат провайдера → восемь полей ``Tenant``.

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
from tenants.models import GeocodeStatus, Tenant

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


@dataclass(frozen=True)
class Applied:
    """Что произошло со строкой. ``status`` пуст, если запись пропущена."""

    tenant_slug: str
    status: GeocodeStatus | None
    skipped_because: str = ""

    @property
    def written(self) -> bool:
        return self.status is not None


def apply_result(
    tenant: Tenant,
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

    current = tenant.geocode_status
    if current == GeocodeStatus.CONFIRMED:
        return Applied(tenant.slug, None, "подтверждено человеком — адаптер не перезаписывает")
    if current == GeocodeStatus.OK and not overwrite_ok:
        return Applied(tenant.slug, None, "уже геокодировано — без --overwrite-ok не трогается")
    if result.outcome is Outcome.UNAVAILABLE and tenant.is_geocoded:
        return Applied(tenant.slug, None, "сервис недоступен — геокодированную строку не портим")

    status = status_for(result, expected_city=tenant.city)
    now = now or timezone.now()

    tenant.geocode_source_address = source_address
    tenant.geocode_normalized_address = result.normalized_address
    tenant.latitude = result.latitude
    tenant.longitude = result.longitude
    tenant.geocode_provider = result.provider
    tenant.geocode_precision = result.provider_precision
    tenant.geocode_status = status
    tenant.geocoded_at = now

    # Правила модели — единственный сторож фикции, и он обязан сработать
    # здесь, а не остаться в admin-форме. Ошибка поднимается наверх: строка
    # с (0,0) от провайдера — это дефект провайдера, и молча превращать её в
    # pending значило бы спрятать его.
    tenant.full_clean(validate_unique=False)

    if not dry_run:
        tenant.save(update_fields=WRITTEN_FIELDS)
    return Applied(tenant.slug, status)
