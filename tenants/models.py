"""Tenant model — multi-tenant foundation (DRF-242.1).

Phase A.7 of the AI Chat foundation. This module is intentionally narrow:
just the Tenant table + manager. Wiring to existing models (FK from
Conversation, User, SpecialistProfile, Appointment, etc.) happens in
follow-up tickets so each conversion is a small, reviewable diff.

Scoping middleware and feature-flag rollout (DRF-242.4/.5) are also
deferred — for now Tenant is purely a registry table.

Why a dedicated app instead of inlining into ``users``:
- Tenant is a cross-cutting domain concept, not a user-system concept.
- Future ticket may add ``TenantSubscription`` / ``TenantBilling`` /
  ``TenantFeatureFlag`` rows without polluting ``users.models``.
- Independent migration history simplifies rollback if the
  multi-tenant rollout has to pause mid-flight.

Why no BaseModel inheritance:
- The codebase uses inline ``UUIDField(primary_key=True, ...)`` per model
  (see ``users.User``, ``ai.Conversation``, ``nutrition.FoodScan``). New
  introduction of a BaseModel would be its own refactor — out of scope.
"""
from __future__ import annotations

import re
import uuid

from django.db import models


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


class _ActiveTenantManager(models.Manager):
    """Default manager: hides ``is_active=False`` tenants from app code.

    Use ``Tenant.all_objects`` in admin / billing to see deactivated rows.
    Pattern matches ``ai.Conversation._ConversationManager`` in this repo.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class GeocodeStatus(models.TextChoices):
    """Исход геокодирования адреса салона (§139, решение владельца 11.09.2026).

    §139 требует хранить «точность и статус результата» и называет два
    исхода, «которые не становятся координатами молча». Отсюда — не
    булево «есть координаты / нет», а набор исходов, каждый со своей
    строкой из решения:

    * ``ok`` — провайдер вернул однозначный результат: «широта и
      долгота, провайдер, точность и статус результата». Единственный
      исход самого геокодера, который считается геокодированным.
    * ``confirmed`` — «Неоднозначный адрес подтверждает человек».
      Кандидата выбрал не геокодер, а человек; исход отдельный, чтобы
      выбор человека не был неотличим от ответа провайдера. Считается
      геокодированным.
    * ``ambiguous`` — «Геокодер не выбирает за него». Провайдер вернул
      несколько кандидатов; координаты (если записаны) — кандидат, а не
      место. НЕ считается.
    * ``pending`` — «При недоступности сервиса адрес сохраняется со
      статусом pending, но в расчёте расстояния НЕ УЧАСТВУЕТ».
      Полноправный исход, а не отсутствие данных. НЕ считается.
    * ``failed`` — сервис ответил, но результата нет: «адрес без
      координат существует, но не притворяется геокодированным». От
      ``pending`` отличается тем, что повтор без изменения адреса
      бессмыслен; от ``ambiguous`` — тем, что подтверждать нечего.
      НЕ считается.

    Пустая строка в ``Tenant.geocode_status`` — «не геокодировался»
    (адрес ни разу не отправляли), по той же конвенции, что пустые
    ``address``/``city``. Это не исход, и он тоже НЕ считается.
    """

    OK = "ok", "Геокодировано"
    CONFIRMED = "confirmed", "Подтверждено человеком"
    AMBIGUOUS = "ambiguous", "Неоднозначно — ждёт человека"
    PENDING = "pending", "Сервис недоступен — ожидает"
    FAILED = "failed", "Не найдено"


# Исходы, при которых координаты считаются координатами места (§139).
# Единственное место, где это решается — ``Tenant.is_geocoded``.
GEOCODED_STATUSES: frozenset[str] = frozenset({
    GeocodeStatus.OK,
    GeocodeStatus.CONFIRMED,
})


class Tenant(models.Model):
    """A logical isolation boundary for marketplace data (DRF-242).

    The MVP shape is deliberately minimal — just enough to scope foreign
    keys later. Pricing, feature flags, branding, etc. are future fields
    or sibling tables.

    Slug is the wire identifier (URL paths, ``X-Tenant`` header values).
    Name is human-readable for admin/UI. ID is the canonical FK target.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(
        max_length=50,
        unique=True,
        help_text=(
            "Lowercase identifier used in URLs and the X-Tenant header. "
            "Letters, digits, hyphen, underscore. Must start with a letter "
            "or digit. Cannot be changed after creation."
        ),
    )
    name = models.CharField(
        max_length=200,
        help_text="Human-readable name shown in admin and billing.",
    )
    # DRF-1587 — «адрес принадлежит салону и мастеру-одиночке» (решение
    # владельца 08.09.2026). До этого тикета Tenant нёс ровно пять полей
    # (id/slug/name/is_active/created_at/updated_at): ни адреса, ни города.
    # Адрес жил только на ``users.SpecialistProfile.address`` и дублировался
    # у каждого мастера салона одной и той же строкой, а город не жил в Ayla
    # вообще — он появлялся только в зеркале бота, куда его вписывал
    # оператор (``create_tenant --city`` / форма подключения салона в
    # админке бота; плюс разовый backfill «Пенза» в миграции зеркала
    # ``tenancy.0010_tenant_city``). То есть город существовал в копии, а
    # не в источнике, и завести настоящий салон с городом через админку
    # Ayla было негде — при том что город является жёстким условием
    # поиска.
    #
    # Пустая строка здесь означает «не указано» и НИКОГДА не должна уезжать
    # наружу как значение: внутренний контракт отдаёт её как ``null``
    # (``_InternalTenantFieldMixin``). Подставлять что-либо по догадке —
    # адрес мастера, город пилота — запрещено: отсутствие обязано доезжать
    # отсутствием (OPEN_DECISIONS §65).
    address = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Адрес салона (или мастера-одиночки), как его показывают "
            "клиенту. Пусто = не указан; наружу уедет null, а не пустая "
            "строка."
        ),
    )
    city = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text=(
            "Город салона. Пусто = не указан; такой салон не попадает ни "
            "в один городской ответ поиска. Наружу уедет null, а не "
            "пустая строка."
        ),
    )
    # DRF-1662 — происхождение координат (§139, решение владельца
    # 11.09.2026). §139 перечисляет, что сохраняется при геокодировании:
    #
    #     исходный и нормализованный адрес
    #     широта и долгота
    #     провайдер
    #     точность и статус результата
    #     время геокодирования
    #
    # Здесь — только место под эту запись. Адаптера геокодера в этом
    # тикете НЕТ; он придёт отдельно и будет писать сюда. Провайдер в
    # записи делает замену провайдера наблюдаемой: без него координаты от
    # двух сервисов лежали бы неразличимо (§139 дословно).
    #
    # Никаких умолчаний-числом: широта и долгота — ``null``, а не ``0.0``
    # (§137: неизвестное расстояние не получает числа; §139: «адрес без
    # координат существует, но не притворяется геокодированным»). Ноль в
    # обеих координатах — точка в Гвинейском заливе, и она прошла бы
    # любую проверку «заполнено».
    #
    # Наличие координат ≠ пригодность. Единственное правило, отличающее
    # «геокодировано» от «есть числа», — ``is_geocoded`` ниже. Читатели
    # (расчёт расстояния, отдача в зеркало) обязаны спрашивать его, а не
    # проверять ``latitude is not None`` сами.
    geocode_source_address = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Адрес в том виде, в каком его отправили геокодеру (снимок "
            "address на момент запроса). Расхождение с текущим address "
            "означает, что координаты устарели."
        ),
    )
    geocode_normalized_address = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Адрес, как его вернул провайдер. Пусто = ответа не было.",
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text=(
            "Широта места оказания услуги. null = неизвестна. Само по "
            "себе число НЕ означает «геокодировано» — см. is_geocoded."
        ),
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text=(
            "Долгота места оказания услуги. null = неизвестна. Само по "
            "себе число НЕ означает «геокодировано» — см. is_geocoded."
        ),
    )
    geocode_provider = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=(
            "Кто вернул координаты (например, yandex). Пусто = никто. "
            "Делает замену провайдера наблюдаемой (§139)."
        ),
    )
    geocode_precision = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=(
            "Точность результата словами провайдера (у Яндекса: exact, "
            "number, near, range, street, other). Хранится дословно, не "
            "интерпретируется здесь."
        ),
    )
    geocode_status = models.CharField(
        max_length=20,
        choices=GeocodeStatus.choices,
        blank=True,
        default="",
        help_text=(
            "Исход геокодирования (§139). Пусто = адрес ни разу не "
            "геокодировали. Геокодированной строка считается ТОЛЬКО при "
            "ok/confirmed — pending, ambiguous и failed с координатами "
            "в расчёте расстояния не участвуют."
        ),
    )
    geocoded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Когда получен этот результат. null = не геокодировали.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "False hides the tenant from default queries. Use this to "
            "soft-disable a tenant without dropping its data — billing "
            "freezes, scoping middleware returns 403."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = _ActiveTenantManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "Тенант"
        verbose_name_plural = "Тенанты"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "slug"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    @property
    def is_geocoded(self) -> bool:
        """Единственное правило «геокодировано», а не «есть числа» (§139).

        True только когда ВСЕ три условия выполнены одновременно:

        1. статус — один из ``GEOCODED_STATUSES`` (``ok`` или
           ``confirmed``). Строка со статусом ``pending``/``ambiguous``/
           ``failed``/пустым геокодированной НЕ считается, даже если в
           ``latitude``/``longitude`` лежат числа: «при недоступности
           сервиса адрес сохраняется со статусом pending, но в расчёте
           расстояния НЕ УЧАСТВУЕТ» (§139);
        2. обе координаты не ``None`` — одна широта без долготы местом
           не является;
        3. пара не равна ``(0, 0)``. Ноль-ноль — точка в Гвинейском
           заливе; для салона в Пензе это не результат геокодера, а
           воскресшее умолчание (§137 снял искусственные числа, и
           «снятая константа возвращается вычисленной» уже случалось).
           Поэтому такая пара читается как отсутствие координат, каким
           бы ни был статус.

        Все читатели (расчёт расстояния, отдача наружу) обязаны
        спрашивать это свойство, а не собирать условия у себя: своя
        копия условий разъедется молча.
        """
        if self.geocode_status not in GEOCODED_STATUSES:
            return False
        if self.latitude is None or self.longitude is None:
            return False
        if self.latitude == 0 and self.longitude == 0:
            return False
        return True

    def clean(self) -> None:
        """Validate slug shape at the model layer.

        Django's SlugField allows uppercase letters and lone hyphens
        (e.g. `-foo-`); we want a stricter shape so slugs round-trip
        cleanly through URL paths and HTTP headers.
        """
        from django.core.exceptions import ValidationError

        super().clean()
        if self.slug and not _SLUG_RE.match(self.slug):
            raise ValidationError({
                "slug": (
                    "Slug must be lowercase alphanumeric (with - or _), "
                    "2–50 chars, and start with a letter or digit."
                ),
            })
