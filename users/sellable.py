"""Одно определение «мастер продаётся клиенту» (DRF-1845, K1a).

До этого модуля условие было записано семь раз (публичный список, три
выборки поиска, движок рекомендаций, источник полки, счётчики полки), и
четыре записи из семи не знали про ``is_booking_enabled``: мастер, который
поставил приём записей на паузу, продолжал продаваться в поиске и в
списке, а клиент упирался в отказ уже на записи
(``create_booking_service``: «Specialist is not accepting bookings»).

Два вопроса, и путать их нельзя:

``catalog_pool_q`` — **в каталоге**: опубликован (``ACTIVE``) и не скрыт
(``is_available``). Этим пулом живут прямая ссылка на профиль и фид для
бота. Фид — именно пул, а не продажа: синк бота только upsert-ит строки и
сам никого не снимает, поэтому мастер на паузе обязан в фиде остаться со
своим ``is_booking_enabled=false`` — иначе в боте останется его старая
активная строка.

``sellable_q`` — **продаётся**: пул ∧ ``is_booking_enabled``. Этим живут
публичные списки, поиск, полка и движок.

Решение владельца (главное окно, 15.09, DRF-1349): пауза приёма —
``is_booking_enabled``; ``is_available`` не трогается.

Сторож класса — ``users/tests/test_sellable_predicate_1845.py``: сырое
``is_available=True`` / ``ProfileStatus.ACTIVE`` в не-тестовом коде вне
этого модуля — красный тест.
"""
from __future__ import annotations

from django.db.models import Q


def _path(prefix: str, field: str) -> str:
    return f"{prefix}__{field}" if prefix else field


def catalog_pool_q(prefix: str = "") -> Q:
    """Опубликован и не скрыт — пул прямой ссылки и фида для бота."""
    from users.models import SpecialistProfile

    return Q(**{
        _path(prefix, "status"): SpecialistProfile.ProfileStatus.ACTIVE,
        _path(prefix, "is_available"): True,
    })


def sellable_q(prefix: str = "") -> Q:
    """Продаётся клиенту: пул и приём записей не на паузе."""
    return catalog_pool_q(prefix) & Q(**{_path(prefix, "is_booking_enabled"): True})


def is_published(profile) -> bool:
    """Профиль опубликован — то, что пишущая дверь «Принимаю записи» требует."""
    from users.models import SpecialistProfile

    return profile.status == SpecialistProfile.ProfileStatus.ACTIVE
