from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from services.normalization import normalize_service_name


class ServiceCategory(models.Model):
    """Hierarchical category for beauty services."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE,
        null=True, blank=True, related_name='children',
    )
    icon = models.CharField(max_length=50, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # tenant FK — DRF-242.6. Categories are tenant-scoped because each
    # marketplace tenant curates its own taxonomy (a beauty salon's
    # "Маникюр" tree differs from a wellness studio's).
    # null=True for legacy / single-tenant rows; backfill in management
    # command. PROTECT to prevent silent loss of taxonomy on tenant
    # deletion — admin must reassign first.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="service_categories",
    )

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'Service Categories'
        # Tenant-scoped taxonomy lookup — DRF-242.7.
        indexes = [
            models.Index(
                fields=['tenant', 'is_active'],
                name='svccat_tenant_active_idx',
            ),
        ]

    def clean(self) -> None:
        """Validate parent: no cycles, max depth 2 (root → subcategory)."""
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError(
                {'parent': 'Category cannot be its own parent.'}
            )
        if self.parent and self.parent.parent_id == self.pk:
            raise ValidationError(
                {'parent': 'Circular parent reference detected.'}
            )
        if self.parent_id and self.parent.parent_id is not None:
            raise ValidationError(
                {'parent': 'Category hierarchy cannot exceed 2 levels.'}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Auto-generate slug from name and run validation before saving."""
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        if self.parent:
            return f"{self.parent.name} → {self.name}"
        return self.name


class ServiceTemplate(models.Model):
    """Предустановленный шаблон услуги, привязанный к категории.

    Используется на онбординге мастера, чтобы предложить готовый список
    популярных услуг с рекомендованной длительностью вместо пустой формы.
    Реальные цены вычисляются через `RegionalPricing` отдельно (DRF-197).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.CASCADE,
        related_name='templates',
    )
    name = models.CharField(max_length=100)
    name_short = models.CharField(
        max_length=40,
        help_text="Короткое имя для чипов/списков",
    )
    # Durations are nullable so the canonical catalog can be seeded from the
    # reference list before per-service timings are curated (see
    # seed_canonical_catalog + docs/CANONICAL_CATALOG_SEED_PLAN_2026-07.md).
    duration_default = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_min = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    duration_max = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    # Canonical health-screening attributes (subset of #200 task 1). A gated
    # service must be screened for contraindications before booking; the bot
    # grounds its health-check on these via ayla_service_id.
    requires_health_check = models.BooleanField(
        default=False,
        help_text="Услуга требует проверки противопоказаний перед записью",
    )
    contraindications = models.TextField(
        blank=True, default="",
        help_text="Противопоказания / оговорки (мед. профиль, разрешение врача и т.п.)",
    )
    is_popular = models.BooleanField(
        default=False,
        help_text="Показывать в верхней части списка категории",
    )
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('category', 'name')]
        ordering = ['-is_popular', 'sort_order', 'name']
        indexes = [
            models.Index(fields=['category', 'is_popular', 'sort_order']),
        ]

    def clean(self) -> None:
        # Durations may be null on canonical rows that are not yet timed;
        # only cross-validate when the pair is present.
        if (
            self.duration_min is not None
            and self.duration_default is not None
            and self.duration_min > self.duration_default
        ):
            raise ValidationError(
                {'duration_min': 'duration_min must be <= duration_default.'}
            )
        if (
            self.duration_max is not None
            and self.duration_default is not None
            and self.duration_max < self.duration_default
        ):
            raise ValidationError(
                {'duration_max': 'duration_max must be >= duration_default.'}
            )

    def __str__(self) -> str:
        return f"{self.name} ({self.category.name})"


class RegionalPricing(models.Model):
    """Рекомендованные ценовые диапазоны для шаблона в конкретном регионе.

    Используется на онбординге мастера, чтобы показать реалистичную вилку.
    Регион определяется через `services.pricing.get_region_key()` по
    приоритету: city_input → reverse-geocoding по координатам → `default`.
    """

    DEFAULT_REGION_KEY = 'default'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        'ServiceTemplate',
        on_delete=models.CASCADE,
        related_name='regional_prices',
    )
    region_key = models.CharField(
        max_length=50,
        help_text="Нормализованный ключ региона: «penza», «moscow», «default»",
    )
    region_name = models.CharField(
        max_length=100,
        help_text="Человекочитаемое имя: «Пенза», «Москва»",
    )
    price_min = models.DecimalField(
        max_digits=8, decimal_places=0,
        validators=[MinValueValidator(1)],
    )
    price_max = models.DecimalField(
        max_digits=8, decimal_places=0,
        validators=[MinValueValidator(1)],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('template', 'region_key')]
        ordering = ['template', 'region_key']
        indexes = [
            models.Index(fields=['region_key']),
        ]

    def clean(self) -> None:
        if self.price_min > self.price_max:
            raise ValidationError(
                {'price_min': 'price_min must be <= price_max.'}
            )

    def __str__(self) -> str:
        return (
            f"{self.template.name} — {self.region_name} "
            f"({self.price_min:.0f}–{self.price_max:.0f} ₽)"
        )


class ServiceTemplateSynonym(models.Model):
    """Подтверждённое название канонической услуги словами салона (§93).

    Зачем
    -----

    Решение владельца §93: когда услугу связывают с каноном, **исходное
    название салона сохраняется как подтверждённый синоним.** Замер
    10.09 показал, зачем это на самом деле нужно, и это не мелочь.

    Из 56 услуг пилотного салона точным совпадением имени с каноном
    находились 8, и вывод был «сорока восьми не с чем сопоставлять».
    Вывод оказался свойством метода. Салон держит вид процедуры **в
    категории**, а канон — в имени::

        салон:  категория «Лазерная эпиляция» + услуга «Подмышки»
        канон:  «Лазерная эпиляция подмышек»               (7.1.6)

    Сравнение имени с именем такую пару найти не может по устройству. В
    каталоге 32 канона лазерной эпиляции, и ручная сверка зона к зоне
    дала **15 из 17**. То есть преобладающая нужда пилота — не создавать
    недостающие каноны, а **записывать синонимы к существующим**.

    Что синоним делает и чего НЕ делает
    -----------------------------------

    Синоним **находит** канон, и на этом его полномочия кончаются.

    Он не создаёт связь, не меняет `SalonService.mapping_status` и никого
    не пускает в подбор. Гейт §76 остаётся единственной дверью:
    `recommendation_eligible = (mapping_status == VERIFIED)`, а `VERIFIED`
    ставит человек, отвечая за это своим именем.

    Разделение намеренное и оно здесь главное. Синоним — **находка**,
    связь — **решение**. Позволить синониму проставлять связь значило бы
    вернуть ровно ту выдумку, против которой §93 и написан: совпадение
    строк снова стало бы доказательством происхождения (§73). Поэтому
    таблица не имеет ни статуса, ни флага «применить»: она отвечает на
    вопрос «как ещё называют вот этот канон», а не «чем является вот эта
    услуга салона».

    Почему провенанс обязателен у каждой строки
    -------------------------------------------

    §93 говорит «**подтверждённый** синоним», и здесь нет второго
    состояния: неподтверждённых синонимов эта таблица не хранит вовсе.
    Значит жизненный цикл не нужен, а провенанс нужен всегда — кто или
    какое правило, когда, на каком основании. Форма та же, что у
    `SalonService`, и по той же причине: запись без автора через месяц
    читается как умолчание.

    Один синоним может вести к нескольким канонам
    ---------------------------------------------

    Ограничение уникальности стоит на паре «шаблон + нормализованный
    текст», а не на тексте одном. То есть «Массаж спины» вправе быть
    синонимом и `1.1.5`, и `1.3.6`.

    **Это не ответ на открытый вопрос владельцу** (ведёт ли синоним к
    одному канону или к нескольким), а отказ отвечать за него кодом.
    Разрешать безопасно: синоним ничего не решает, и оператор, увидев
    двух кандидатов, выбирает сам. Запрещать было бы опаснее — второй
    салон не смог бы записать своё настоящее название, не удалив чужое.
    Если владелец скажет «один канон» — это одна миграция с
    `UniqueConstraint` на `normalized`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(
        ServiceTemplate,
        on_delete=models.CASCADE,
        related_name="synonyms",
    )
    #: Как это называется у салона — дословно, включая регистр и знаки.
    #: Хранится нетронутым: оператор должен видеть исходную строку, а не
    #: её обработанный след.
    text = models.CharField(max_length=200)
    #: Ключ поиска. Заполняется в `save()` из `text`, руками не вводится.
    normalized = models.CharField(max_length=200, editable=False, db_index=True)
    #: Чьё это название. Не обязателен: синоним может прийти из
    #: справочника или из разбора, а не от конкретного салона.
    source_tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    #: Провенанс. Та же форма, что у `SalonService`: кто ИЛИ правило.
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    confirmed_rule = models.CharField(max_length=100, blank=True, default="")
    rule_version = models.CharField(max_length=32, blank=True, default="")
    confirmed_at = models.DateTimeField()
    source_ref = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["template", "text"]
        constraints = [
            # Один и тот же синоним дважды у одного шаблона — дубль.
            # Уникальность по НОРМАЛИЗОВАННОМУ тексту, иначе «Подмышки»
            # и «подмышки » считались бы разными записями и обе висели
            # бы в выдаче.
            models.UniqueConstraint(
                fields=["template", "normalized"],
                name="templatesynonym_template_normalized_uniq",
            ),
            # Провенанс обязателен у КАЖДОЙ строки: неподтверждённых
            # синонимов эта таблица не хранит (§93). `confirmed_at`
            # закрыт `NOT NULL` самим полем, здесь — остальные два.
            models.CheckConstraint(
                condition=(
                    ~models.Q(source_ref="")
                    & (
                        models.Q(confirmed_by__isnull=False)
                        | ~models.Q(confirmed_rule="")
                    )
                ),
                name="templatesynonym_requires_provenance",
            ),
            # Кто ИЛИ правило, но не оба — как у связи. Оба заполненных
            # означают, что происхождение известно неточно.
            models.CheckConstraint(
                condition=~(
                    models.Q(confirmed_by__isnull=False)
                    & ~models.Q(confirmed_rule="")
                ),
                name="templatesynonym_provenance_is_who_xor_rule",
            ),
            # Правило без версии — «подтверждено какой-то из версий».
            models.CheckConstraint(
                condition=(
                    models.Q(confirmed_rule="")
                    | ~models.Q(rule_version="")
                ),
                name="templatesynonym_rule_carries_version",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Нормализованный ключ считается ЗДЕСЬ, а не в форме и не в
        # вызывающем коде: строка может приехать миграцией, командой или
        # админкой, и ключ обязан получиться один и тот же во всех трёх
        # случаях. Поле `editable=False` именно поэтому — вводить его
        # руками некому.
        self.normalized = normalize_service_name(self.text)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.text} → {self.template.name}"


class Service(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        'users.SpecialistProfile',
        on_delete=models.CASCADE,
        related_name='services',
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='services',
    )
    # tenant FK — DRF-242.6. Denormalised from specialist.tenant so
    # marketplace listing queries can filter by tenant_id without JOIN
    # to SpecialistProfile. Invariant maintained by backfill + service
    # layer (see docs/MULTI_TENANT.md).
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="services",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(1)],
    )
    duration_minutes = models.PositiveIntegerField(
        validators=[MinValueValidator(15), MaxValueValidator(480)],
    )
    image = models.ImageField(
        upload_to='services/', blank=True, null=True,
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    buffer_after_minutes = models.PositiveSmallIntegerField(
        default=0,
        help_text="Required gap (minutes) after this service before next booking",
    )

    # B9 T+2h aftercare push body. Founder pilot safety rule
    # (project_pilot_scope_discipline): NO LLM-generated care advice
    # pilot — only approved canonical text per service. Empty value
    # is the default and suppresses the push entirely (explicit opt-in
    # per service when ops curator fills approved content via admin).
    # Sent VERBATIM by notifications.dispatch_post_visit_aftercare;
    # no template-side embellishment beyond a static title prefix.
    aftercare_text = models.TextField(
        blank=True, default='',
        help_text=(
            "Approved post-visit aftercare advice. Sent verbatim via "
            "B9 T+2h push. Empty (default) suppresses the push."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        # Composite indexes for the catalog filter patterns:
        # - filter by specialist + category + active state (catalog browse)
        # - filter by specialist + price range (price slider, "от-до")
        # Plain indexes on (specialist) and (category) alone aren't enough —
        # the planner needs combined coverage for the WHERE/AND chains the
        # catalog generates from query params.
        indexes = [
            models.Index(
                fields=['specialist', 'category', 'is_active'],
                name='svc_spec_cat_active_idx',
            ),
            models.Index(
                fields=['specialist', 'price'],
                name='svc_spec_price_idx',
            ),
            # Marketplace listing — DRF-242.7. Filter tenant first to
            # narrow the candidate set before any specialist/category
            # predicates kick in.
            models.Index(
                fields=['tenant', 'is_active'],
                name='svc_tenant_active_idx',
            ),
        ]

    def __str__(self):
        return f"{self.name} — {self.specialist.display_name}"


# --------------------------------------------------------------------------- #
# S3A canonical catalog rebuild (#1044 / #200) — additive new layer.
# ServiceTemplate (taxonomy) -> SalonService -> SpecialistService (bookable).
# Service / Appointment are intentionally NOT touched here (strangler-fig;
# cutover is the separate founder-authorized S3-CUT chunk). See
# docs/CATALOG_DOMAIN_REBUILD_S3_DESIGN_2026-07.md.
# --------------------------------------------------------------------------- #
class SalonService(models.Model):
    """A service a salon (Tenant) offers — mid layer of the catalog chain.

    Derived from a ``ServiceTemplate`` (taxonomy) or, for off-taxonomy
    custom offerings, standalone with an explicit category (D2). Not
    bookable on its own — a ``SpecialistService`` makes it bookable.
    """

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        YCLIENTS = "yclients", "YClients"
        SEED = "seed", "Seed"

    class MappingStatus(models.TextChoices):
        """Статус связи услуги с каноническим шаблоном. §76, расширен §93.

        Четыре состояния, и путать их запрещено::

            UNMAPPED         REVIEW_REQUIRED  VERIFIED         NOT_RECOMMENDABLE
            валидной связи   связь есть, но   подтверждена     решено, что связи
            ещё нет          происхождения    человеком ЛИБО   НЕ БУДЕТ
                             недостаточно     правилом
                |                  |                |                  |
            не участвует ----------+                |          не участвует
            в подборе                               |          в подборе
                                        участвует в подборе

        **`UNMAPPED` и `NOT_RECOMMENDABLE` — не одно и то же**, хотя на
        гейте ведут себя одинаково. Это третий исход разбора по §93, и
        отличается он не поведением, а тем, что за ним стоит::

            UNMAPPED           про строку ещё никто ничего не сказал
            NOT_RECOMMENDABLE  человек посмотрел и решил; у решения есть
                               автор, дата и основание

        Отсутствие и отказ совпадают ровно один раз — когда гейт их не
        пускает. Дальше они расходятся: `UNMAPPED` — очередь работы,
        `NOT_RECOMMENDABLE` — работа сделанная. Слитые в одно, они
        превращают убывающую очередь в вечную: пятьдесят шесть
        разобранных услуг пилота возвращались бы в неё каждым отчётом, и
        по переписи было бы не видно, что разбор вообще шёл.

        Поэтому у отказа своё `CheckConstraint` на происхождение — такое
        же, как у `VERIFIED`, и по той же причине: **решение без автора
        через месяц читается как умолчание.**

        **Ноль `VERIFIED` не разрешает откат на `REVIEW_REQUIRED`**
        (формулировка владельца): иначе статус декоративен, а система
        продолжает выдавать непроверенные связи. Пустая выдача при нуле
        подтверждённых — штатное состояние с именем, а не поломка.

        Умолчание — `UNMAPPED`, а не `REVIEW_REQUIRED`. Разница смысловая:
        строка, про которую ещё ничего не сказано, не должна выглядеть как
        строка, про которую сказано «связь есть».
        """

        UNMAPPED = "unmapped", "Unmapped"
        REVIEW_REQUIRED = "review_required", "Review required"
        VERIFIED = "verified", "Verified"
        NOT_RECOMMENDABLE = "not_recommendable", "Не подлежит рекомендациям"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="salon_services",
    )
    # Nullable so off-taxonomy custom services are allowed (D2); when null,
    # ``category`` is required (enforced in clean()).
    template = models.ForeignKey(
        "ServiceTemplate",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="salon_services",
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="salon_services",
    )
    name = models.CharField(max_length=200)
    # Salon-level default; null resolves from template (see
    # SpecialistService.resolved_duration).
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    base_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        validators=[MinValueValidator(1)],
    )
    # Escalate-only floor vs template (D1): admin may set True on a salon
    # even when the template does not require it; cannot relax a gated
    # template downstream (SpecialistService.resolved_requires_health_check).
    #
    # ТРЁХЗНАЧНОЕ, и третье состояние несущее::
    #
    #     NULL   салон на вопрос НЕ ОТВЕЧАЛ
    #     True   салон говорит: скрининг нужен
    #     False  салон говорит: скрининг НЕ нужен
    #
    # Двузначным поле быть не может, и это выяснилось на живом решении.
    # Владелец постановил (§90) размечать пилотный каталог явными ответами
    # салона вместо умолчаний кода — и оказалось, что сказать «нет» салону
    # нечем: поставленный человеком `False` был неотличим от `False`,
    # которого никто не касался. Решение было бы исполнимо наполовину:
    # «да» записывалось бы, «нет» пропадало молча, а услуга навсегда
    # оставалась бы «неизвестной» и уезжала оператору.
    #
    # Это тот же инвариант, который на этой границе уже проведён дважды —
    # у зеркала (`MasterService.resolved_requires_health_check`) и у самого
    # вердикта: **отсутствие обязано быть отличимо от значения.** Здесь, у
    # источника признака, он оставался непроведённым дольше всех.
    requires_health_check = models.BooleanField(null=True, blank=True, default=None)
    is_active = models.BooleanField(default=True)
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.MANUAL,
    )

    # -- Статус связи с шаблоном и его происхождение (§76) -------------------
    #
    # `source` выше говорит, откуда взялась СТРОКА (ручной ввод, YClients,
    # сид). Поля ниже говорят, чем доказана СВЯЗЬ с каноническим шаблоном.
    # Это разные вопросы: строка из YClients может нести связь, которую
    # никто не проверял, а строка, заведённая руками, — связь, выбранную
    # человеком осознанно.
    mapping_status = models.CharField(
        max_length=24,
        choices=MappingStatus.choices,
        default=MappingStatus.UNMAPPED,
    )
    #: Кто подтвердил. Взаимоисключающе с `mapping_confirmed_rule`:
    #: владелец назвал «кто ИЛИ какое правило», и оба сразу означали бы,
    #: что происхождение неизвестно точно.
    mapping_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    #: Имя детерминированного правила, если подтверждал не человек.
    mapping_confirmed_rule = models.CharField(max_length=100, blank=True, default="")
    #: Версия правила. Без неё «подтверждено правилом» неотличимо от
    #: «подтверждено какой-то из его версий», а правило меняется.
    mapping_rule_version = models.CharField(max_length=32, blank=True, default="")
    mapping_confirmed_at = models.DateTimeField(null=True, blank=True)
    #: Ссылка на исходное основание — драфт, выгрузка, решение, тикет.
    #: Свободная строка намеренно: оснований разных видов, и требовать
    #: одного типа значило бы запретить те, которых мы ещё не видели.
    mapping_source_ref = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "template", "name"],
                name="salonservice_tenant_template_name_uniq",
            ),
            # `VERIFIED` без provenance невозможен НА УРОВНЕ СХЕМЫ, а не по
            # договорённости. §76 требует хранить, кто или какое правило
            # подтвердило, когда и по какому основанию; статус без этого —
            # то же самое, что объяснение без evidence (§73), а именно так
            # литерал рейтинга однажды и стал «проверенным фактом».
            #
            # Проверка стоит здесь, а не в `clean()`, потому что `clean()`
            # обходится любым `update()` и любой миграцией данных.
            models.CheckConstraint(
                condition=(
                    ~models.Q(mapping_status="verified")
                    | (
                        models.Q(mapping_confirmed_at__isnull=False)
                        & ~models.Q(mapping_source_ref="")
                        & (
                            models.Q(mapping_confirmed_by__isnull=False)
                            | ~models.Q(mapping_confirmed_rule="")
                        )
                    )
                ),
                name="salonservice_verified_requires_provenance",
            ),
            # Отказ — тоже решение, и провенанс ему нужен по той же
            # причине, что и подтверждению (§93). Форма условия
            # намеренно та же, что у `verified` выше: два терминальных
            # состояния связи, и оба обязаны отвечать на «кто, когда, на
            # каком основании».
            #
            # Отдельным ограничением, а не расширением верхнего через
            # `IN (verified, not_recommendable)`: имя ограничения — это
            # то, что читает человек в тексте ошибки, и «нарушено
            # not_recommendable_requires_provenance» говорит ему, какое
            # именно решение он пытается записать без автора.
            models.CheckConstraint(
                condition=(
                    ~models.Q(mapping_status="not_recommendable")
                    | (
                        models.Q(mapping_confirmed_at__isnull=False)
                        & ~models.Q(mapping_source_ref="")
                        & (
                            models.Q(mapping_confirmed_by__isnull=False)
                            | ~models.Q(mapping_confirmed_rule="")
                        )
                    )
                ),
                name="salonservice_not_recommendable_requires_provenance",
            ),
            # Правило без версии — «подтверждено какой-то из версий».
            # Человеку версия не нужна: он и есть провенанс.
            models.CheckConstraint(
                condition=(
                    models.Q(mapping_confirmed_rule="")
                    | ~models.Q(mapping_rule_version="")
                ),
                name="salonservice_rule_confirmation_carries_version",
            ),
            # Кто ИЛИ правило, но не оба. Второй инвариант этой
            # миграции, и он чинит РАСХОЖДЕНИЕ, а не добавляет новое:
            # докстринг `mapping_confirmed_by` называл поля
            # взаимоисключающими со ссылкой на §76 с самого начала, а
            # оба ограничения выше написаны через `OR` и обе заполненные
            # строки пропускали. То есть решение владельца было записано
            # и не исполнялось, а выглядело исполненным —
            # «проверяемый инвариант не живёт в комментарии».
            #
            # Условие глобальное, а не только для терминальных
            # состояний: происхождение, набранное на строке, которая ещё
            # не решена, тоже обязано быть однозначным — иначе
            # неоднозначность просто дожидается смены статуса.
            models.CheckConstraint(
                condition=~(
                    models.Q(mapping_confirmed_by__isnull=False)
                    & ~models.Q(mapping_confirmed_rule="")
                ),
                name="salonservice_provenance_is_who_xor_rule",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="salonsvc_tenant_active_idx",
            ),
            models.Index(
                fields=["tenant", "category", "is_active"],
                name="salonsvc_tenant_cat_active_idx",
            ),
            # Допустимость подбора спрашивает статус на каждом запросе,
            # а очередь проверки выбирает по нему же.
            models.Index(
                fields=["tenant", "mapping_status"],
                name="salonsvc_tenant_mapstatus_idx",
            ),
        ]

    def clean(self) -> None:
        if self.template_id is None and self.category_id is None:
            raise ValidationError(
                {"category": "category is required for off-taxonomy custom services."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.name} @ {self.tenant.slug}"


class SpecialistService(models.Model):
    """A specialist performs a SalonService — the BOOKABLE catalog unit.

    ``id`` is the stable booking key the bot resolves (see the stable-id
    contract in the design doc). ``duration_minutes`` / ``requires_health_check``
    resolve down the chain (specialist -> salon -> template).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.PROTECT,
        related_name="specialist_services",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        related_name="specialist_services",
    )
    # Denormalized from salon_service for tenant-scoped queries; populated in
    # save() when unset. Nullable for parity with other denormalized tenant FKs.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="specialist_services",
    )
    # Nullable in DB; must be resolvable when is_active (enforced in clean()).
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(480)],
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(1)],
    )
    # ДВУЗНАЧНОЕ — сознательно, и это решение, а не недосмотр рядом с
    # трёхзначным полем салона.
    #
    # У мастера по D1 есть право только ПОДНЯТЬ признак и нет права его
    # опустить: ослабить пол шаблона или ответ салона он не может. Значит
    # высказывание «мастер говорит: не нужно» в модели не существует, и
    # третьему состоянию нечего было бы означать. `False` здесь — полный
    # ответ («не эскалирую»), а не молчание.
    #
    # У салона иначе: он — авторитет по собственным внетаксономическим
    # услугам и вправе отвечать в обе стороны, поэтому его поле обязано
    # различать «нет» и «не отвечал».
    #
    # Половинчатая трёхзначность была бы хуже последовательной
    # двузначности: читатель не знал бы, чему верить.
    requires_health_check = models.BooleanField(default=False)
    buffer_after_minutes = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["specialist", "salon_service"],
                name="specialistservice_specialist_salon_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "is_active"],
                name="specsvc_tenant_active_idx",
            ),
            models.Index(
                fields=["specialist", "is_active"],
                name="specsvc_spec_active_idx",
            ),
            models.Index(
                fields=["salon_service", "is_active"],
                name="specsvc_salon_active_idx",
            ),
        ]

    def resolved_duration(self) -> int | None:
        """First non-null of specialist -> salon -> template duration."""
        if self.duration_minutes is not None:
            return self.duration_minutes
        salon = self.salon_service
        if salon.duration_minutes is not None:
            return salon.duration_minutes
        template = salon.template
        if template is not None:
            return template.duration_default
        return None

    def resolved_requires_health_check(self) -> bool | None:
        """Escalate-only OR across template floor, salon, specialist (D1).

        Tri-state. ``None`` means **unknown**, not ``False``.

        Precedence, and it is deliberate:

        1. An explicit raise anywhere wins. ``salon.requires_health_check``
           or ``self.requires_health_check`` being ``True`` returns ``True``
           even with no template — escalate-only is preserved exactly.
        2. With a template, its flag is the canonical floor and the answer
           is a real ``bool``.
        3. **Without a template, and with nobody having raised the flag,
           the answer is ``None``.** There is no canonical floor to read,
           so the honest answer is "not known".

        Why case 3 is not ``False``. The previous version wrote
        ``template.requires_health_check if template is not None else False``,
        which turned *the absence of a canonical link* into *a positive
        claim about safety*. Measured on the pilot 09.09.2026: of 387 active
        bookable edges, 96 resolved ``False`` solely because no template was
        attached — 95 of them the pilot salon's, i.e. every edge a real
        person can book there. The salon had not answered the question; the
        cascade answered it for the salon.

        Consumers must decide what to do with ``None`` explicitly. The bot
        mirror already models it — ``MasterService.resolved_requires_health_check``
        is ``null=True`` and its booking gate treats ``NULL`` as "screening
        required" — and never saw a ``NULL`` only because this method never
        produced one.
        """
        salon = self.salon_service
        template = salon.template
        template_floor = (
            template.requires_health_check if template is not None else None
        )

        # 1. Поднятый пол шаблона не снимает никто — это и есть D1
        #    «escalate-only»: салон не вправе ослабить канон.
        #
        #    СРОК ГОДНОСТИ У ЭТОГО ШАГА. Он трактует поднятый пол
        #    БЕЗУСЛОВНО — не потому, что так решено, а потому, что
        #    различить нечем: у `ServiceTemplate.requires_health_check`
        #    нет поля происхождения. Решение владельца §95 (10.09.2026):
        #    гейт, выведенный правилом, — черновой, и скрининга по нему
        #    не требуем; просмотренный человеком — требуем. Сегодня из
        #    102 гейтованных шаблонов человеком не просмотрен ни один
        #    (85 выведены членством в подкатегории, 17 — словом в
        #    названии), и сид про себя говорит «draft flags for later
        #    owner review», а поля под этот review не существует.
        #
        #    Как только провенанс шаблона появится, ЭТОТ ШАГ ОБЯЗАН
        #    измениться: черновой пол перестаёт быть безусловным. Пока
        #    поля нет, безусловность — граница знания, а не решение, и
        #    принимать её за решение нельзя.
        if template_floor is True:
            return True
        # 2. Эскалация мастера. Он вправе поднять и не вправе опустить,
        #    поэтому поле остаётся двузначным — см. его докстринг.
        if self.requires_health_check:
            return True
        # 3. Ответ салона, если салон отвечал. Трёхзначное поле: `False`
        #    здесь — это сказанное «нет», а не молчание.
        if salon.requires_health_check is not None:
            return bool(salon.requires_health_check)
        # 4. Шаблон есть и флага не несёт — ответил канон.
        if template_floor is False:
            return False
        # 5. Опоры нет и никто не отвечал. Отсутствие свидетельства не
        #    является свидетельством безопасности.
        return None

    def clean(self) -> None:
        if self.is_active and self.resolved_duration() is None:
            raise ValidationError(
                {"duration_minutes": "An active bookable service needs a resolvable duration."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.tenant_id is None and self.salon_service_id is not None:
            self.tenant_id = self.salon_service.tenant_id
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.salon_service.name} — {self.specialist.display_name}"


class DraftSalonService(models.Model):
    """External-prefill staging row for onboarding "Confirm, don't create".

    An intake (YClients API-pull or CSV-bootstrap) writes a draft; a human
    confirms it -> materializes a SalonService (+ ExternalSourceMapping).
    The confirm/reject WRITE flow lands in S3C — S3A ships the model only.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    class ExternalSource(models.TextChoices):
        YCLIENTS = "yclients", "YClients"
        CSV = "csv", "CSV bootstrap"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="draft_salon_services",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    external_source = models.CharField(
        max_length=10, choices=ExternalSource.choices,
        default=ExternalSource.YCLIENTS,
    )
    external_service_id = models.CharField(max_length=64, blank=True, default="")
    suggested_template = models.ForeignKey(
        "ServiceTemplate",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    external_name = models.CharField(max_length=200)
    suggested_duration = models.PositiveIntegerField(null=True, blank=True)
    suggested_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    confirmed_salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Idempotent intake key — only when an external id is present, so
            # multiple manual/blank drafts coexist.
            models.UniqueConstraint(
                fields=["tenant", "external_source", "external_service_id"],
                condition=~models.Q(external_service_id=""),
                name="draftsalonservice_external_id_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "status"],
                name="draftsvc_tenant_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Draft<{self.status}> {self.external_name} @ {self.tenant.slug}"


class ExternalSourceMapping(models.Model):
    """Idempotent key between an external system id and an Ayla entity.

    Keyed by YClients ``service_id`` / ``staff_id`` (per tenant, since
    YClients ids are per-company). Two explicit nullable FKs rather than a
    GenericForeignKey (D6): ``external_type`` discriminates which is set.
    Guarantees a re-import re-uses the same Ayla id.
    """

    class Source(models.TextChoices):
        YCLIENTS = "yclients", "YClients"

    class ExternalType(models.TextChoices):
        SERVICE = "service", "Service"
        STAFF = "staff", "Staff"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.YCLIENTS,
    )
    external_type = models.CharField(max_length=8, choices=ExternalType.choices)
    external_id = models.CharField(max_length=64)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="external_source_mappings",
    )
    salon_service = models.ForeignKey(
        "SalonService",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="external_source_mappings",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="external_source_mappings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_type", "external_id", "tenant"],
                name="externalsourcemapping_key_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant", "source", "external_type"],
                name="extmap_tenant_src_type_idx",
            ),
        ]

    def clean(self) -> None:
        """Exactly one target FK, matching external_type."""
        if self.external_type == self.ExternalType.SERVICE:
            if self.salon_service_id is None or self.specialist_id is not None:
                raise ValidationError(
                    "external_type 'service' requires salon_service and no specialist."
                )
        elif self.external_type == self.ExternalType.STAFF:
            if self.specialist_id is None or self.salon_service_id is not None:
                raise ValidationError(
                    "external_type 'staff' requires specialist and no salon_service."
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.source}:{self.external_type}:{self.external_id} @ {self.tenant.slug}"


class ExternalBusyInterval(models.Model):
    """An external (e.g. YClients) busy interval the slot busy-guard subtracts.

    Source-abstracted (S3-CAL): YClients is coupled only in the webhook ingress.
    A Variant-A (Ayla-primary) pivot leaves this table unfed by YClients — the
    slot engine and recheck-at-confirm read it identically regardless of source.
    See docs/CATALOG_EXTERNAL_BUSY_S3CAL_DESIGN_2026-07.md.
    """

    class Source(models.TextChoices):
        YCLIENTS = "yclients", "YClients"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        related_name="external_busy_intervals",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile",
        on_delete=models.CASCADE,
        related_name="external_busy_intervals",
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.YCLIENTS,
    )
    external_id = models.CharField(max_length=64, blank=True, default="")
    raw_payload = models.JSONField(default=dict, blank=True)
    # Set by the webhook ingress to the ingestion time (staleness signal for
    # recheck); null until an ingress writes it.
    received_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id", "tenant"],
                condition=~models.Q(external_id=""),
                name="externalbusyinterval_ext_id_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["specialist", "start_at", "end_at"],
                name="extbusy_spec_window_idx",
            ),
        ]

    def clean(self) -> None:
        if self.end_at is not None and self.start_at is not None and self.end_at <= self.start_at:
            raise ValidationError(
                {"end_at": "end_at must be after start_at."}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"busy[{self.source}] {self.specialist_id} "
            f"{self.start_at:%Y-%m-%d %H:%M}–{self.end_at:%H:%M}"
        )


# --------------------------------------------------------------------------- #
# Goal layer (DRF-1190 / OD-1, 2026-08-19) — additive new layer.
# Курируемые подсказки целей и маппинг «цель → категории услуг» как ДАННЫЕ,
# заполняемые владельцем (тот же паттерн, что ServiceTemplate / RegionalPricing:
# экспертное знание живёт в каталоге, не в коде скоринга). Наполнение —
# management command + services/seeds/*.json, не миграции и не админка-вручную.
# --------------------------------------------------------------------------- #
class GoalOption(models.Model):
    """Курируемая подсказка цели («чип») для слоя «цель → услуга».

    Не является меню экрана: сервер отдаёт активные подсказки как часть
    документа состояния, экран их только отрисовывает. Свободный текст
    остаётся равноправным вводом (OD-1) и здесь не хранится — за него
    отвечает goals.ClientGoal.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(
        max_length=64,
        unique=True,
        help_text="Стабильный ключ подсказки; уходит в ClientGoal.goal_key и события воронки",
    )
    label = models.CharField(
        max_length=100,
        help_text="Человекочитаемая подпись чипа (рус.)",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'key']
        indexes = [
            models.Index(
                fields=['is_active', 'sort_order'],
                name='goaloption_active_sort_idx',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.key})"


class GoalOptionCategory(models.Model):
    """Связь «подсказка цели → категория услуг» — курируемое знание владельца.

    Резолвер (goals.resolution) отображает активную цель клиента в набор
    category_id через эту таблицу; движок рекомендаций о целях не знает.
    Одна подсказка может вести в несколько категорий; порядок выдачи —
    sort_order.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    goal_option = models.ForeignKey(
        GoalOption,
        on_delete=models.CASCADE,
        related_name='category_links',
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.PROTECT,
        related_name='goal_option_links',
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('goal_option', 'category')]
        ordering = ['goal_option', 'sort_order']
        indexes = [
            models.Index(
                fields=['goal_option', 'sort_order'],
                name='goaloptcat_option_sort_idx',
            ),
        ]

    def __str__(self) -> str:
        return f"{self.goal_option.key} → {self.category.name}"
