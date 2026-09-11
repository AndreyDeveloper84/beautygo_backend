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

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
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
    """Исход геокодирования адреса — решение владельца §139 (11.09.2026).

    §139 перечисляет, что именно надо сохранять («точность и статус
    результата»), и отдельным разделом называет **два исхода, которые не
    становятся координатами молча**. Из этого следует, что «статус» — не
    булево «получилось / не получилось»: получилось-не-получилось не
    вмещает ни ожидания сервиса, ни ожидания человека.

    Каждое значение ниже обосновано строкой §139, а не удобством:

    ``NOT_ATTEMPTED``
        «для разового заполнения координат уже существующих адресов» —
        §139 прямо предусматривает, что адреса живут в базе ДО того, как
        к ним приходил геокодер. Умолчание поля: адрес есть, геокодер не
        вызывался ни разу. Отличать это от «вызывался и не вышло»
        обязательно — иначе прогон по существующим адресам не сможет
        выбрать себе вход.

    ``OK``
        «исходный и нормализованный адрес / широта и долгота / провайдер /
        точность и статус результата / время геокодирования» — полный
        набор от провайдера, однозначный результат. Единственный исход,
        порождённый геокодером, который §139 разрешает считать
        геокодированным.

    ``AMBIGUOUS``
        «Неоднозначный адрес подтверждает ЧЕЛОВЕК. Геокодер не выбирает за
        него.» Провайдер вернул несколько кандидатов. Координаты в строке
        МОГУТ быть заполнены (лучшим кандидатом — как справочное
        значение), но выбор ещё не сделан, поэтому строка НЕ
        геокодирована.

    ``CONFIRMED``
        Вторая половина той же строки §139: подтверждает человек. Исход
        отделён от ``OK`` намеренно — иначе «выбрал геокодер» и «выбрал
        человек» легли бы неразличимо, ровно той же болезнью, от которой
        §139 лечит поле ``geocode_provider`` («без него координаты от двух
        разных сервисов лежали бы неразличимо»). Считается
        геокодированным.

    ``PENDING``
        «При недоступности сервиса адрес сохраняется со статусом
        ``pending``, но в расчёте расстояния НЕ УЧАСТВУЕТ.» Дословный
        исход из §139, и единственный, чьё имя владелец задал сам. Это
        полноправный исход, а не отсутствие данных: адрес сохранён,
        повтор запланирован.

    ``FAILED``
        §139 называет особыми ровно ДВА исхода (неоднозначность и
        недоступность). Значит обычный отказ — сервис ответил, адреса
        нет — существует отдельно от них: он не ждёт ни человека, ни
        починки сервиса. Слив его в ``PENDING`` заставил бы повторять
        вечно то, что не изменится; слив в ``AMBIGUOUS`` позвал бы
        человека выбирать из пустого списка.

    Геокодированной строку делают только ``OK`` и ``CONFIRMED`` — см.
    ``Tenant.GEOCODED_STATUSES`` и ``Tenant.is_geocoded``.
    """

    NOT_ATTEMPTED = "not_attempted", "Геокодер не вызывался"
    OK = "ok", "Успех — однозначный результат провайдера"
    AMBIGUOUS = "ambiguous", "Неоднозначно — ждёт подтверждения человеком"
    CONFIRMED = "confirmed", "Подтверждено человеком"
    PENDING = "pending", "Сервис недоступен — повтор запланирован"
    FAILED = "failed", "Отказ провайдера — адрес не найден"


class Tenant(models.Model):
    """A logical isolation boundary for marketplace data (DRF-242).

    The MVP shape is deliberately minimal — just enough to scope foreign
    keys later. Pricing, feature flags, branding, etc. are future fields
    or sibling tables.

    Slug is the wire identifier (URL paths, ``X-Tenant`` header values).
    Name is human-readable for admin/UI. ID is the canonical FK target.
    """
    #: Исходы §139, при которых строка СЧИТАЕТСЯ геокодированной.
    #: Множество, а не одно значение: геокодер и человек — разные
    #: источники выбора, но оба дают пригодные координаты. Остальные
    #: исходы (``pending`` / ``ambiguous`` / ``failed`` /
    #: ``not_attempted``) не становятся пригодными от того, что в строке
    #: оказались числа.
    GEOCODED_STATUSES = frozenset({
        GeocodeStatus.OK.value,
        GeocodeStatus.CONFIRMED.value,
    })

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
    # ── DRF-1662 · происхождение координат (§139 от 11.09.2026) ────────
    # §139 «Что сохраняется»: исходный и нормализованный адрес, широта и
    # долгота, провайдер, точность и статус результата, время
    # геокодирования. Семь полей ниже — построчный перенос этого списка.
    #
    # Адаптера геокодера в этом тикете НЕТ. Здесь только место, куда он
    # будет писать, и одно правило (``is_geocoded``), отличающее
    # «геокодировано» от «в строке есть числа».
    #
    # Координаты — ``null``, а не ``0.0``. §137 отменил искусственные
    # ``0.5`` для неизвестного расстояния; здесь то же на уровне входа.
    # Ноль в широте и долготе — точка в Гвинейском заливе, и она прошла бы
    # любую проверку «заполнено».
    geocode_source_address = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Строка адреса ровно в том виде, в каком её отдали геокодеру. "
            "Хранится отдельно от ``address``, потому что ``address`` "
            "редактируем: без снимка входа нельзя сказать, устарели ли "
            "координаты. Пусто = геокодер ещё не звали."
        ),
    )
    geocode_normalized_address = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Адрес, каким его вернул провайдер (§139 «нормализованный "
            "адрес»). Показывать человеку при подтверждении "
            "неоднозначного адреса. Пусто = провайдер ничего не вернул."
        ),
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
        help_text=(
            "Широта места оказания услуги. NULL = неизвестна. Никаких "
            "умолчаний-чисел: 0.0 здесь — значение, а не «пусто»."
        ),
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
        help_text=(
            "Долгота места оказания услуги. NULL = неизвестна. Никаких "
            "умолчаний-чисел: 0.0 здесь — значение, а не «пусто»."
        ),
    )
    geocode_provider = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=(
            "Кто выдал координаты: ``yandex`` для пилота, ``manual`` для "
            "подтверждённых человеком. §139: провайдер в записи делает "
            "замену провайдера НАБЛЮДАЕМОЙ — без него координаты от двух "
            "сервисов лежат неразличимо. Пусто = координат нет."
        ),
    )
    geocode_precision = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=(
            "Точность результата словарём провайдера (§139 «точность ... "
            "результата»); у Яндекса это exact/number/near/range/street/"
            "other. Словарь провайдерский намеренно: приводить его к "
            "общему виду — работа адаптера, которого здесь ещё нет, и "
            "приведение без адаптера потеряло бы исходный ответ. Пусто = "
            "провайдер точность не сообщил."
        ),
    )
    geocode_status = models.CharField(
        max_length=16,
        choices=GeocodeStatus.choices,
        default=GeocodeStatus.NOT_ATTEMPTED,
        help_text=(
            "Исход геокодирования (§139). Только ``ok`` и ``confirmed`` "
            "делают строку геокодированной; ``pending`` — полноправный "
            "исход «сервис недоступен», а не отсутствие данных, и в "
            "расчёте расстояния не участвует."
        ),
    )
    geocoded_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Когда геокодер в последний раз отвечал по этому адресу "
            "(§139 «время геокодирования»). NULL = не отвечал ни разу. "
            "Заполняется при ЛЮБОМ исходе, включая pending и failed: "
            "иначе повторный прогон не отличит «не звали» от «звали час "
            "назад и сервис лежал»."
        ),
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
        """Пригодны ли координаты этой строки (§139) — ЕДИНСТВЕННОЕ место.

        Правило живёт здесь, а не у каждого читателя: своя копия условий
        разъезжается молча, и первый же, кто напишет
        ``if t.latitude is not None``, вернёт ``pending`` в расчёт
        расстояния — ровно то, что §139 запрещает дословно.

        Строка геокодирована, когда выполнено ВСЁ:

        1. статус — из ``GEOCODED_STATUSES`` (§139: ``pending`` «в расчёте
           расстояния НЕ УЧАСТВУЕТ»; ``ambiguous`` ждёт человека);
        2. заполнены ОБЕ координаты — одна широта без долготы точки не
           задаёт;
        3. координаты не ``0.0 / 0.0``.

        Про пункт 3 — это решение, а не самоочевидность. ``0.0 / 0.0`` —
        точка в Гвинейском заливе, и она прошла бы и «заполнено», и любую
        проверку диапазона. Это ровно та форма, в которой возвращается
        выброшенное умолчание: §137 снял искусственные ``0.5`` для
        неизвестного расстояния, а снятая константа имеет привычку
        возвращаться вычисленной. Салона на нулевом меридиане в Гвинейском
        заливе быть не может, а «ноль как умолчание» — может. Поэтому
        ``0.0 / 0.0`` здесь читается как отсутствие, а не как место.
        Запись такой пары дополнительно запрещена в ``clean()``, так что
        пункт 3 — вторая линия, а не единственная.
        """
        if self.geocode_status not in self.GEOCODED_STATUSES:
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

        # DRF-1662 / §139 — контракт отказа на границе записи. Проверка в
        # ``is_geocoded`` спасает от уже лежащей в базе фикции; три отказа
        # ниже не дают её туда положить.
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError({
                "latitude": (
                    "Широта и долгота заполняются только вместе: одна "
                    "координата точки не задаёт."
                ),
            })
        if (
            self.latitude is not None
            and self.longitude is not None
            and self.latitude == 0
            and self.longitude == 0
        ):
            raise ValidationError({
                "latitude": (
                    "0.0 / 0.0 — точка в Гвинейском заливе, а не адрес. "
                    "Неизвестные координаты — NULL (§137, §139)."
                ),
            })
        if self.geocode_status in self.GEOCODED_STATUSES and (
            self.latitude is None or self.longitude is None
        ):
            raise ValidationError({
                "geocode_status": (
                    f"Статус «{self.geocode_status}» означает успешное "
                    "геокодирование, но координат в строке нет. Неудача — "
                    "это pending / ambiguous / failed."
                ),
            })


# `ServiceLocation` (§9, DRF-1687) живёт в отдельном модуле, чтобы этот файл не
# рос дальше; импорт здесь — чтобы Django увидел модель при загрузке приложения.
from .service_location import LocationStatus, ServiceLocation  # noqa: E402,F401
