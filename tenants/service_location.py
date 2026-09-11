"""`ServiceLocation` — место оказания услуги (решение владельца §9, 11.09.2026; DRF-1687).

Зачем отдельная сущность, если у `Tenant` уже есть адрес и координаты (#333)
------------------------------------------------------------------------

Владелец: «все старые адреса перестают быть авторитетными… расстояние
считается до точки конкретного предложения, а не до профиля мастера.
Модель: `Master → works_at → ServiceLocation → address + coordinates`».

Адрес у `Tenant` — **вход**: то, что салон назвал. Место — **подтверждённая
точка**, у которой есть статус, автор подтверждения и координаты с
происхождением. Соло-мастер вне салона тоже оказывает услуги в месте, а
тенанта у него может не быть — поэтому `tenant` здесь допускает `NULL`.

Восемь полей провенанса координат — **перенос** из `Tenant` (#333), с тем же
правилом `is_geocoded` и теми же отказами: половина пары и (0, 0) не
записываются. Правило одно, живёт здесь; `Tenant` оставит `address`/`city`
как вход и потеряет координаты, когда читателей у них не останется (L8).

Два статуса, две оси
--------------------

``status`` — про **место**: подтверждено ли, что услуги оказывают здесь.
``geocode_status`` — про **координаты**: что ответил геокодер. Оси
независимы: подтверждённое место может быть ещё не геокодировано, а
геокодированный адрес — ещё не подтверждён человеком. В расчёте расстояния
участвует только пересечение — ``participates_in_distance``.

Что здесь НЕТ, и почему
-----------------------

* ``SpecialistProfile.works_at`` — срез L2, отдельный PR;
* переноса данных — миграция только добавляет таблицу. §9: «автоматический
  массовый перенос запрещён», перенос по одному — команда среза L3;
* ``SpecialistService.location`` — мастера с двумя местами на пилоте нет,
  а пустой FK «на будущее» — та же фикция, что нули в координатах.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .models import GeocodeStatus, Tenant


class LocationStatus(models.TextChoices):
    """Подтверждено ли место — §9 дословно, три исхода."""

    CONFIRMED = "confirmed", "Подтверждено"
    REVIEW_REQUIRED = "review_required", "Требует проверки"
    INACTIVE = "inactive", "Недействительно"


class ServiceLocation(models.Model):
    #: Исходы геокодера, при которых координаты пригодны (§139) — как у Tenant.
    GEOCODED_STATUSES = frozenset({GeocodeStatus.OK.value, GeocodeStatus.CONFIRMED.value})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, null=True, blank=True, related_name="locations",
        help_text="Салон, которому принадлежит место. NULL — самостоятельный мастер вне салона.",
    )
    label = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Как место называют люди: «Салон на Пушкина», «Кабинет 3». Пусто — берётся адрес.",
    )

    # -- Вход геокодера -----------------------------------------------------
    address = models.CharField(max_length=500, help_text="Адрес места, как его назвали. Вход геокодера.")
    city = models.CharField(max_length=100, blank=True, default="")

    # -- Восемь полей §139 — перенос из Tenant (#333), построчно ---------------
    geocode_source_address = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Адрес в том виде, в каком его отдали геокодеру. Пусто = геокодер ещё не звали.",
    )
    geocode_normalized_address = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Адрес, каким его вернул провайдер. Пусто = провайдер ничего не вернул.",
    )
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
        help_text="NULL = неизвестна. 0.0 здесь — значение, а не «пусто».",
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
        help_text="NULL = неизвестна. 0.0 здесь — значение, а не «пусто».",
    )
    geocode_provider = models.CharField(max_length=50, blank=True, default="")
    geocode_precision = models.CharField(max_length=32, blank=True, default="")
    geocode_status = models.CharField(
        max_length=16, choices=GeocodeStatus.choices, default=GeocodeStatus.NOT_ATTEMPTED,
    )
    geocoded_at = models.DateTimeField(null=True, blank=True)

    # -- Подтверждение места (§9) — с провенансом, как §1 п. 7 ---------------
    status = models.CharField(
        max_length=16, choices=LocationStatus.choices, default=LocationStatus.REVIEW_REQUIRED,
        help_text=(
            "confirmed — здесь оказывают услуги, подтверждено человеком; "
            "review_required — происхождение неизвестно; inactive — тестовый, личный, недействительный."
        ),
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", help_text="Кто подтвердил место. Обязателен при confirmed.",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_source_ref = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Основание: ссылка на решение, переписку, документ. Обязательно при confirmed.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Место оказания услуг"
        verbose_name_plural = "Места оказания услуг"
        ordering = ["tenant__name", "label", "address"]
        indexes = [models.Index(fields=["status", "geocode_status"])]
        constraints = [
            # §9 п. 7-подобное: подтверждение без автора, времени и основания
            # невозможно на уровне схемы, не только формы.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="confirmed")
                    | (
                        models.Q(confirmed_at__isnull=False)
                        & models.Q(confirmed_by__isnull=False)
                        & ~models.Q(confirmed_source_ref="")
                    )
                ),
                name="servicelocation_confirmed_requires_provenance",
            ),
            # Половина пары точки не задаёт — схемой, а не только clean():
            # update() и bulk_create() clean() не зовут.
            models.CheckConstraint(
                condition=(
                    (models.Q(latitude__isnull=True) & models.Q(longitude__isnull=True))
                    | (models.Q(latitude__isnull=False) & models.Q(longitude__isnull=False))
                ),
                name="servicelocation_coordinates_are_a_pair",
            ),
            # (0, 0) — Гвинейский залив, а не адрес. Неизвестное — NULL.
            models.CheckConstraint(
                condition=~(models.Q(latitude=0) & models.Q(longitude=0)),
                name="servicelocation_no_zero_zero",
            ),
            # «Совпавшие точки связываются без создания дублей» (§9):
            # одна точка на (салон, нормализованный адрес). NULL-тенант
            # (соло) участвует в уникальности как значение, а не как
            # «всегда различно» — иначе у соло-мастеров дубли не ловились бы.
            models.UniqueConstraint(
                fields=["tenant", "geocode_normalized_address"],
                condition=~models.Q(geocode_normalized_address=""),
                nulls_distinct=False,
                name="servicelocation_point_is_unique",
            ),
        ]

    def __str__(self) -> str:
        head = self.label or self.address
        return f"{head} ({self.tenant.slug})" if self.tenant_id else f"{head} (соло)"

    @property
    def is_geocoded(self) -> bool:
        """Пригодны ли координаты — то же правило, что у Tenant (#333), одно место."""
        if self.geocode_status not in self.GEOCODED_STATUSES:
            return False
        if self.latitude is None or self.longitude is None:
            return False
        if self.latitude == 0 and self.longitude == 0:
            return False
        return True

    @property
    def participates_in_distance(self) -> bool:
        """Единственное условие, при котором расстояние до этого места считается.

        Две оси сходятся здесь и только здесь: место подтверждено человеком
        (``status``) И координаты пригодны (``is_geocoded``). Всё остальное —
        ``DISTANCE_UNKNOWN`` (§8), а не ноль и не нейтраль.
        """
        return self.status == LocationStatus.CONFIRMED and self.is_geocoded

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if (self.latitude is None) != (self.longitude is None):
            errors["latitude"] = "Широта и долгота заполняются только вместе."
        if self.latitude is not None and self.longitude is not None and self.latitude == 0 and self.longitude == 0:
            errors["latitude"] = "0.0 / 0.0 — точка в Гвинейском заливе, а не адрес. Неизвестные координаты — NULL."
        if self.geocode_status in self.GEOCODED_STATUSES and (self.latitude is None or self.longitude is None):
            errors["geocode_status"] = (
                f"Статус «{self.geocode_status}» означает успешное геокодирование, но координат нет."
            )
        if self.status == LocationStatus.CONFIRMED:
            if self.confirmed_by_id is None:
                errors["confirmed_by"] = "Подтверждённому месту нужен тот, кто подтвердил."
            if self.confirmed_at is None:
                errors["confirmed_at"] = "Подтверждённому месту нужно время подтверждения."
            if not self.confirmed_source_ref:
                errors["confirmed_source_ref"] = "Подтверждённому месту нужно основание."
        if not (self.address or "").strip():
            errors["address"] = "Место без адреса — не место."
        if errors:
            raise ValidationError(errors)
