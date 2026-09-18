"""Горизонт бронирования — один источник для слотов и записи (DRF-2081).

``BOOKING_MAX_AHEAD_DAYS`` читали пять мест напрямую из settings: политика
создания записи, салонное окно ``CreateBookingService``, инвалидация кэша
слотов (``schedule_api``, ``admin``) — а слот-эндпоинт не читал вовсе. Слот
за горизонтом показывался, запись на него отказывала (находка DRF-2014).

Здесь — единственный читатель настройки. Всё остальное зовёт
:func:`booking_horizon_end` и сравнивает с ОДНИМ И ТЕМ ЖЕ мгновением: слоты
на граничном дне отсекаются там же, где политика создания начинает
отказывать, — не «дата ≤ N дней», а тот же instant. Перепись читателей
строки ``BOOKING_MAX_AHEAD_DAYS`` держит тест
``users/tests/test_slots_booking_horizon_2081.py::TestOneSource``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.conf import settings

#: Запасное значение — то же, что в settings.base; сюда попадает только при
#: отсутствии настройки, и тест переписи не даёт завести второе число.
DEFAULT_BOOKING_MAX_AHEAD_DAYS = 60


def booking_horizon_days() -> int:
    """Сколько дней вперёд открыто бронирование."""
    return int(getattr(settings, "BOOKING_MAX_AHEAD_DAYS", DEFAULT_BOOKING_MAX_AHEAD_DAYS))


def booking_horizon_end(now: datetime | None = None) -> datetime:
    """Последнее мгновение, на которое ещё можно записаться (UTC-aware).

    Слот с ``start_at`` позже этого мгновения не показывается; запись с
    ``start_at`` позже него отказывает ``BookingWindowError``.
    """
    reference = now if now is not None else datetime.now(tz=timezone.utc)
    return reference + timedelta(days=booking_horizon_days())


__all__ = ["DEFAULT_BOOKING_MAX_AHEAD_DAYS", "booking_horizon_days", "booking_horizon_end"]
