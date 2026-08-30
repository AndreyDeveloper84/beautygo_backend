"""Review model — client feedback on completed appointments."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Review(models.Model):
    """A review left by a client after a completed appointment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # OneToOne: one review per appointment
    appointment = models.OneToOneField(
        'appointments.Appointment',
        on_delete=models.CASCADE,
        related_name='review',
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews_written',
    )
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='reviews',
    )
    # DRF-1421. Каталог живёт в двух слоях одновременно (незавершённая
    # strangler-fig миграция, чанк S3-CUT — см. ``services/models.py``), и
    # отзыв обязан уметь сослаться на тот слой, в котором лежит услуга его
    # брони. Форма ссылок повторяет ``Appointment`` буквально: обнуляемая
    # маркетплейсная ``service`` XOR обнуляемая салонная ``salon_service``,
    # ровно одна из двух — CHECK ``review_exactly_one_service_source``.
    #
    # Почему копия ``Appointment``, а не «переезд на канон»: отзыв — это
    # денормализация брони (``Review`` — OneToOne к ``Appointment``), и
    # одинаковая форма делает запись прямым копированием без разбора
    # случаев. Легаси при этом не выключается: маркетплейсный тенант через
    # Pro-приложение пишет ровно в ``Service`` — как и в #267, это
    # объединение, а не замена.
    #
    # Что это даёт S3-CUT: убрать ``Service`` из отзывов будет ровно той же
    # операцией, что и из броней, — DROP COLUMN плюс схлопывание CHECK до
    # ``salon_service IS NOT NULL``. Одна каноническая ссылка вместо пары
    # потребовала бы переноса маркетплейсных отзывов, обобщённая — потери
    # ссылочной целостности и переписывания content-type на катофере.
    service = models.ForeignKey(
        'services.Service',
        on_delete=models.CASCADE,
        related_name='reviews',
        null=True, blank=True,
    )
    # CASCADE — та же политика, что у соседней ``service``: держать обе ноги
    # XOR одинаковыми важнее, чем повторить PROTECT из ``Appointment``. На
    # практике ветка недостижима: ``Appointment`` PROTECT-ит
    # ``SalonService``, а отзыв без брони не существует, — значит удалить
    # услугу «из-под» отзыва нельзя в принципе.
    salon_service = models.ForeignKey(
        'services.SalonService',
        on_delete=models.CASCADE,
        related_name='reviews',
        null=True, blank=True,
    )

    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    text = models.TextField(blank=True, max_length=1000)

    is_anonymous = models.BooleanField(
        default=False,
        help_text="Hide client name in public listing",
    )
    specialist_reply = models.TextField(
        blank=True, null=True,
        help_text="Specialist response to this review",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="Hidden by moderation — excluded from rating",
    )

    # tenant FK — DRF-242.6. Denormalised from appointment.specialist.tenant
    # so per-tenant aggregates (avg rating, review counts) avoid a 3-hop JOIN.
    # PROTECT against silent data loss on tenant delete; invariant maintained
    # by backfill + service layer.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviews",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Отзыв'
        verbose_name_plural = 'Отзывы'
        indexes = [
            models.Index(fields=['specialist', 'is_hidden', '-created_at'],
                         name='review_specialist_listing_idx'),
            models.Index(fields=['client'], name='review_client_idx'),
            # Per-tenant review aggregates — DRF-242.7. Marketplace
            # admin pulls recent reviews by tenant for moderation.
            models.Index(
                fields=['tenant', '-created_at'],
                name='review_tenant_created_idx',
            ),
        ]
        constraints = [
            # DRF-1421 — ровно одна типизированная ссылка на услугу:
            # маркетплейсная ``service`` XOR салонная ``salon_service``.
            # Оба NULL и оба заполнены отвергаются на уровне БД. Близнец
            # ``appointment_exactly_one_service_source``; отзыв копирует
            # ссылку из своей брони, поэтому инвариант тот же самый.
            #
            # Существующие строки (``service`` заполнен, salon NULL) валидны
            # как есть — миграции данных нет.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        service__isnull=False, salon_service__isnull=True,
                    ) | models.Q(
                        service__isnull=True, salon_service__isnull=False,
                    )
                ),
                name='review_exactly_one_service_source',
            ),
        ]

    @property
    def service_name(self) -> str:
        """Название услуги из того слоя каталога, в котором она лежит.

        Наружный контракт (``service_name`` в листинге и в ответе на
        создание) — строка, а не ``null``. Без этого разрешения салонный
        отзыв отдавал бы ``null``: ``source='service.name'`` через NULL-FK
        возвращает ``None``, и регрессия прошла бы молча.

        Пустая строка недостижима под CHECK ``review_exactly_one_service_
        source`` и оставлена как честный ответ вместо падения.
        """
        if self.service_id is not None:
            return self.service.name
        if self.salon_service_id is not None:
            return self.salon_service.name
        return ""

    def __str__(self) -> str:
        return f"Review by {self.client_id} — {self.rating}★ for {self.specialist_id}"
