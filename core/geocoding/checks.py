"""Системная проверка: обратное геокодирование не притворяется живым (DRF-1685).

Три месяца региональная цена по координатам «работала»: функция была,
тесты зелёные, а ключ существовал только в тестах — в бою всегда пусто,
всегда ``default``, и ни одна строка об этом не говорила. Механизм
построен и выключен — это **третье состояние**, и оно обязано быть видно.

Проверка запускается на каждом ``manage.py check / migrate / runserver``:

* провайдер настроен — молчит;
* не настроен и ``GEOCODING_REQUIRE_LIVE_REVERSE = False`` — **Warning**
  ``geocoding.W001``: состояние названо, запуск не блокируется;
* не настроен и ``= True`` («функция объявлена живой») — **Error**
  ``geocoding.E001``: ровно сторож из тикета — краснеет, когда ключа нет, а
  функция объявлена живой.

Проверка не ходит в сеть: она читает то же, что читает ``check()``
провайдера, — настройки.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

from services.geocoding import reverse_geocoding_readiness


@register()
def reverse_geocoding_is_not_a_fiction(app_configs, **kwargs):
    reason = reverse_geocoding_readiness()
    if reason is None:
        return []
    msg = (
        f"Обратное геокодирование (региональная цена по координатам) не настроено: "
        f"{reason}. Пока так — региональная цена по координатам всегда default."
    )
    if getattr(settings, "GEOCODING_REQUIRE_LIVE_REVERSE", False):
        return [Error(msg, hint="Задайте ключ провайдера или снимите GEOCODING_REQUIRE_LIVE_REVERSE.",
                      id="geocoding.E001")]
    return [Warning(msg, hint="Это известное состояние (DRF-1685), а не сбой.", id="geocoding.W001")]
