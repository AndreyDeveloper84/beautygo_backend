"""Создание отзыва — одна реализация для двух дверей (DRF-1855, карта кабинета K12).

Двери две: клиентское приложение (``POST /api/v1/reviews/``, JWT клиента) и
внутренний контур бота под субъектом (``POST /internal/users/{user_id}/reviews/``).
Правила отзыва не должны расходиться между ними — своя завершённая запись,
один отзыв на визит, пара «услуга» из брони, пересчёт рейтинга в той же
транзакции, — поэтому они живут здесь, а двери различаются только тем, кто
назвал клиента.
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction
from django.db.models import Avg, Count

from appointments.models import Appointment

from .models import Review

logger = logging.getLogger(__name__)


class ReviewRefused(Exception):
    """Отказ с кодом и статусом — вид переводит его в ответ как есть."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(code, message, status_code)
        self.code = code
        self.message = message
        self.status_code = status_code


def recalculate_rating(specialist) -> None:
    """Пересчитать ``rating`` и ``reviews_count`` мастера по видимым отзывам.

    Синхронно, внутри транзакции создания отзыва.
    """
    result = Review.objects.filter(
        specialist=specialist, is_hidden=False,
    ).aggregate(avg=Avg('rating'), cnt=Count('id'))
    avg = result['avg'] or 0
    cnt = result['cnt'] or 0
    specialist.__class__.objects.filter(id=specialist.id).update(
        rating=round(avg, 1),
        reviews_count=cnt,
    )
    logger.info(
        "Rating recalculated: specialist=%s rating=%.1f count=%d",
        specialist.id, avg, cnt,
    )


def create_review(
    *,
    client,
    appointment_id: UUID,
    rating: int,
    text: str = '',
    is_anonymous: bool = False,
) -> Review:
    """Создать отзыв клиента о его завершённом визите.

    Бросает :class:`ReviewRefused`:

    * ``NOT_FOUND`` 404 — нет такой брони **у этого клиента** (чужая бронь
      неотличима от несуществующей);
    * ``APPOINTMENT_NOT_COMPLETED`` 400 — визит не завершён;
    * ``REVIEW_EXISTS`` 409 — отзыв на этот визит уже есть.
    """
    try:
        appointment = Appointment.objects.select_related(
            'specialist', 'service', 'salon_service',
        ).get(id=appointment_id, client=client)
    except Appointment.DoesNotExist:
        raise ReviewRefused("NOT_FOUND", "Appointment not found.", 404) from None

    if appointment.status != Appointment.Status.COMPLETED:
        raise ReviewRefused(
            "APPOINTMENT_NOT_COMPLETED",
            "Reviews can only be left for completed appointments.",
            400,
        )

    if Review.objects.filter(appointment=appointment).exists():
        raise ReviewRefused(
            "REVIEW_EXISTS",
            "A review for this appointment already exists.",
            409,
        )

    with transaction.atomic():
        review = Review.objects.create(
            appointment=appointment,
            client=client,
            specialist=appointment.specialist,
            # DRF-1421 — прямое копирование пары ссылок из брони: XOR уже
            # соблюдён на ``Appointment``, ``Review`` несёт тот же CHECK.
            service=appointment.service,
            salon_service=appointment.salon_service,
            rating=rating,
            text=text,
            is_anonymous=is_anonymous,
        )
        recalculate_rating(appointment.specialist)

    logger.info(
        "Review created: id=%s specialist=%s rating=%d",
        review.id, review.specialist_id, review.rating,
    )
    return review
