from __future__ import annotations

import logging
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify

from core.image_privacy import MetadataFreeImageField
from services.canonical_code import validate_canonical_code
from services.normalization import normalize_service_name

logger = logging.getLogger(__name__)


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
        # DRF-2726: перенос подкатегории под другой корень меняет область
        # компетенции рецензента у всех её процедур.
        was_parent = self._stored_parent_if_moved()
        # Запись и сброс — одной транзакцией: иначе при сбое сброса перенос
        # уже лежит в базе, повторное сохранение разницы не увидит, и
        # подтверждения переживут перенос.
        with transaction.atomic():
            super().save(*args, **kwargs)
            if was_parent is not None:
                names = dict(
                    type(self).objects.filter(pk__in=[was_parent[0], self.parent_id]).values_list("pk", "name")
                )
                change = {
                    "field": "category.parent",
                    "old": names.get(was_parent[0], "—"),
                    "new": names.get(self.parent_id, "—"),
                }
                for template in ServiceTemplate.objects.filter(category=self):
                    reset_claims_of_procedure(
                        template, reason=ClaimApprovalReset.Reason.CATEGORY_MOVED, changes=[change],
                    )
                # DRF-2741: противопоказание, чья область названа этой
                # категорией либо корнем, который она покинула или под который
                # пришла, — состав процедур в его области изменился. Новый
                # корень отдельно не называется: он — родитель этой категории.
                reset_contraindications(
                    contraindications_in_scope(category_ids=[self.pk, was_parent[0]]),
                    reason=ClaimApprovalReset.Reason.CATEGORY_MOVED, changes=[change],
                )

    def _stored_parent_if_moved(self) -> tuple[Any] | None:
        """``(прежний parent_id,)``, если это сохранение переносит категорию; иначе ``None``."""
        if self._state.adding:
            return None
        stored = type(self).objects.filter(pk=self.pk).values_list("parent_id", flat=True)[:1]
        if not stored or stored[0] == self.parent_id:
            return None
        return (stored[0],)

    def __str__(self):
        if self.parent:
            return f"{self.parent.name} → {self.name}"
        return self.name


class ServiceTemplate(models.Model):
    """Каноническая услуга. В коде — шаблон, в разговорах — канон.

    Используется на онбординге мастера, чтобы предложить готовый список
    популярных услуг с рекомендованной длительностью вместо пустой формы.
    Реальные цены вычисляются через `RegionalPricing` отдельно (DRF-197).

    С §93 справочник перестал быть замороженным закрытым списком и стал
    **курируемым расширяемым реестром**: нет подходящего канона —
    оператор заводит новый. Отсюда жизненный цикл, см. `Lifecycle`.
    """

    class Lifecycle(models.TextChoices):
        """Состояние самой канонической услуги. Решение владельца §93.

        Не путать с `SalonService.MappingStatus`: тот про **связь**
        услуги салона с каноном, этот — про **канон**. Оси разные, и
        вопросы разные::

            MappingStatus   «чем доказано, что вот эта услуга салона —
                             вот этот канон»
            Lifecycle       «проверял ли кто-нибудь, что вот этот канон
                             вообще должен существовать»

        `PROVISIONAL` — оператор завёл его на ходу, потому что
        подходящего не нашлось (§93, шаг 1). Это рабочее состояние, а не
        брак: без него разбор упирается в закрытый справочник, ровно как
        упёрся на пилоте.

        `APPROVED` — кто-то проверил и отвечает за это именем или
        правилом. Провенанс обязателен и стоит `CheckConstraint`'ом:
        одобрение без автора через месяц читается как умолчание.

        **Умолчание — `PROVISIONAL`**, и это не придирка. Канон,
        заведённый кодом, миграцией или чужой рукой, никем не проверен
        по определению. Умолчание `APPROVED` означало бы, что каждая
        новая строка сама себя одобрила.

        ### Body Care CAT-2: ещё два состояния, та же ось

        Контракт Body Care v0.1 §2.1 требует у канона статус
        DRAFT / CANDIDATE / ACTIVE / RETIRED. Решение (главное окно, 06.10):
        расширить эту ось, а не заводить вторую. Сопоставление::

            контракт   здесь
            DRAFT      provisional   — существующее значение, не переименовано
            CANDIDATE  candidate     — предложен к активации, ждёт проверки
            ACTIVE     approved      — существующее значение, не переименовано
            RETIRED    retired       — выведен из оборота

        Существующие значения не переименованы: это миграция данных по
        всем строкам ради слова. Все читатели оси спрашивают ``== approved``
        и поэтому читают оба новых состояния как «не одобрено» — новое
        значение безопасно по умолчанию.

        ``retired`` — решение, и провенанс у него тот же, что у одобрения:
        дата, основание и «кто ИЛИ правило» (CheckConstraint'ы ниже).
        ``candidate`` провенанса не требует: это предложение, а не решение.

        **Чего вывод из оборота НЕ делает сам.** Рекомендуемость сегодня —
        ``SalonService.mapping_status == VERIFIED``, и статус канона она не
        видит: связь салона с выведенным каноном остаётся в подборе.
        Закрыть это — связать допустимость со статусом (CAT-10); до тех
        пор такие связи считает ``check_canon_invariants``
        (``verified_on_retired``), чтобы дыра не молчала.
        """

        PROVISIONAL = "provisional", "Черновой"
        CANDIDATE = "candidate", "Кандидат"
        APPROVED = "approved", "Одобрен"
        RETIRED = "retired", "Выведен"

    class ServiceFamily(models.TextChoices):
        """Семейство body-care услуги — контракт Body Care v0.1 §2.1 (CAT-1).

        Четыре значения первой версии контракта, и только они: семейство
        решает, какие факты конфигурации салон обязан назвать (CAT-5) и
        какие вопросы скрининга задаются (бот). Услуга вне body-care —
        стрижка, маникюр — семейства не имеет: ``NULL``, а не пятое
        значение «прочее». «Прочее» читалось бы как классифицированное.

        Значения — строчные, как у остальных перечислений этого файла;
        написание контракта (``BODY_WRAP``) — имя члена. Перевод на провод
        — дело API (CAT-11), а не хранения.
        """

        BODY_WRAP = "body_wrap", "Обёртывание"
        SPA_BODY = "spa_body", "SPA-уход за телом"
        MECHANICAL_SCRUB = "mechanical_scrub", "Механический скраб"
        ACID_CARE = "acid_care", "Кислотный уход"

    class BodyCareScope(models.TextChoices):
        """Подлежит ли канон контракту Body Care — классификация, не допуск.

        Решение владельца 07.10: отсутствие семейства перестаёт значить
        «вне Body Care». ``NULL`` — классификация **неизвестна**, и
        неизвестное не допускается; «вне Body Care» — положительное
        значение, которое кто-то подтвердил.

        ``NOT_BODY_CARE`` говорит только то, что проверка конфигурации
        Body Care к канону не применяется. О юридическом классе,
        противопоказаниях и готовности оно не говорит ничего — их
        проверяют свои гейты.
        """

        BODY_CARE = "body_care", "Подлежит Body Care"
        NOT_BODY_CARE = "not_body_care", "Вне Body Care"

    class LegalServiceClass(models.TextChoices):
        """Юридический класс услуги в РФ — контракт v0.2 §7A.1 (§7A-0).

        Ровно четыре значения контракта. ``NULL`` — класс не установлен; и
        ``NULL``, и ``LEGAL_REVIEW_REQUIRED`` читатель понимает как «не
        разрешено» (fail-closed). Какой процедуре какой класс — решение
        юриста (D-1); здесь значения никому не присваиваются.
        """

        NON_MEDICAL_COSMETIC = "non_medical_cosmetic", "Немедицинская косметическая"
        MEDICAL_COSMETOLOGY = "medical_cosmetology", "Медицинская косметология"
        MEDICAL_OTHER = "medical_other", "Иная медицинская"
        LEGAL_REVIEW_REQUIRED = "legal_review_required", "Нужна юридическая проверка"

    class PractitionerClass(models.TextChoices):
        """Требуемая квалификация исполнителя — контракт v0.2 §7A.4 (§7A-0).

        Пять значений «минимума» контракта. ``NULL`` — требование не
        установлено (не «любой мастер»). Какая квалификация нужна какой
        процедуре — решение клиники (D-2).
        """

        COSMETIC_ESTHETICIAN = "cosmetic_esthetician", "Косметик-эстетист"
        NURSE_COSMETOLOGY = "nurse_cosmetology", "Медсестра по косметологии"
        PHYSICIAN_COSMETOLOGIST = "physician_cosmetologist", "Врач-косметолог"
        MEDICAL_SPECIALIST = "medical_specialist", "Врач-специалист"
        PROTOCOL_SPECIFIC = "protocol_specific", "По протоколу"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.CASCADE,
        related_name='templates',
    )
    name = models.CharField(max_length=100)
    # OD-MAP-05 (MAP-AUTO-01): стабильная ЛОГИЧЕСКАЯ идентичность строки
    # эталонного справочника владельца — «1.3.24». `id` — технический PK и
    # идентичностью между базами не является. Контракт целиком —
    # `services/canonical_code.py`: формат N.N.N; уникален среди непустых;
    # NULL = «канон не из эталонного списка» (40 строк seed_service_templates
    # DRF-196 и PROVISIONAL-каноны оператора — без кода навсегда);
    # неизменяем после установки (clean() и save() ниже); из имени или
    # категории НЕ выводится — источник только seed (bootstrap 0024).
    canonical_code = models.CharField(
        max_length=16, null=True, blank=True,
        validators=[validate_canonical_code],
        help_text=(
            "Код эталонного справочника (N.N.N). Пусто — канон не из эталонного списка. "
            "После установки не меняется."
        ),
    )
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

    class HealthCheckOrigin(models.TextChoices):
        #: Выведено правилом (членство в подкатегории, слово в названии) —
        #: черновик, человек флаг не смотрел. Так стоят все строки засева.
        INFERRED = "inferred", "Inferred"
        #: Флаг просмотрен и подтверждён человеком или названным правилом.
        CONFIRMED = "confirmed", "Confirmed"

    #: DRF-2614. Происхождение ФЛАГА `requires_health_check` — не строки
    #: справочника (для неё — `lifecycle`/`approved_*` ниже). Решение
    #: владельца §95: выведенный гейт — черновой, просмотренный — нет; до
    #: этого поля их нечем было различить. Здесь только РАЗЛИЧЕНИЕ: гейт
    #: записи по-прежнему отказывает по поднятому флагу любого
    #: происхождения, пока владелец не скажет иначе.
    health_check_origin = models.CharField(
        max_length=16,
        choices=HealthCheckOrigin.choices,
        default=HealthCheckOrigin.INFERRED,
    )
    #: Кто подтвердил флаг. Взаимоисключающе с `health_check_confirmed_rule`
    #: — «кто ИЛИ какое правило», та же форма, что у одобрения строки (§93).
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    health_check_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="+",
    )
    health_check_confirmed_rule = models.CharField(max_length=100, blank=True, default="")
    health_check_rule_version = models.CharField(max_length=32, blank=True, default="")
    health_check_confirmed_at = models.DateTimeField(null=True, blank=True)
    health_check_source_ref = models.CharField(max_length=200, blank=True, default="")

    contraindications = models.TextField(
        blank=True, default="",
        help_text="Противопоказания / оговорки (мед. профиль, разрешение врача и т.п.)",
    )
    is_popular = models.BooleanField(
        default=False,
        help_text="Показывать в верхней части списка категории",
    )
    sort_order = models.PositiveIntegerField(default=0)

    # -- Жизненный цикл самого канона (§93) --------------------------------
    lifecycle = models.CharField(
        max_length=16,
        choices=Lifecycle.choices,
        default=Lifecycle.PROVISIONAL,
    )
    #: Кто одобрил. Взаимоисключающе с `approved_rule` — «кто ИЛИ какое
    #: правило», та же форма, что у связи (§76).
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="+",
    )
    approved_rule = models.CharField(max_length=100, blank=True, default="")
    approval_rule_version = models.CharField(max_length=32, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    #: Основание одобрения — разбор, реестр владельца, тикет.
    approval_source_ref = models.CharField(max_length=200, blank=True, default="")
    #: Body Care CAT-2: кто вывел канон из оборота. Та же форма, что у
    #: одобрения: «кто ИЛИ правило», правило с версией, дата и основание.
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="+",
    )
    retired_rule = models.CharField(max_length=100, blank=True, default="")
    retirement_rule_version = models.CharField(max_length=32, blank=True, default="")
    retired_at = models.DateTimeField(null=True, blank=True)
    retirement_source_ref = models.CharField(max_length=200, blank=True, default="")

    # -- Body Care: семейство и версии (контракт v0.1 §2.1, CAT-1) ----------
    # Решение владельца D-3 (06.10): расширять ServiceTemplate, а не заводить
    # отдельный CanonicalService. Здесь — только фундамент: что за семейство
    # и по каким версиям канона и политик оно оценивалось. Скрининг,
    # пределы (LIM) и классификатор кислот сюда не входят — они ждут
    # решений клиники и юриста (D-1, D-2, D-4).
    service_family = models.CharField(
        max_length=24,
        choices=ServiceFamily.choices,
        null=True,
        blank=True,
        help_text="Семейство body-care; пусто — услуга вне body-care",
    )
    # -- Область классификации (решение владельца 07.10) -------------------
    # Отдельная ось рядом с семейством: ``NULL`` — неизвестно, и читатель
    # обязан понимать это как «не допущено» (``body_care_scope.scope_of``).
    # ``body_care`` без семейства допустимо — «подлежит, семейство не
    # определено»: услуга тела вне четырёх семейств контракта записывается
    # честно и остаётся закрытой. Семейство без ``body_care`` база не
    # примет (CheckConstraint ниже).
    body_care_scope = models.CharField(
        max_length=16,
        choices=BodyCareScope.choices,
        null=True,
        blank=True,
        help_text="Подлежит ли Body Care; пусто — классификация неизвестна, читается как «не допущено»",
    )
    #: Кто подтвердил область. Взаимоисключающе с ``scope_confirmed_rule`` —
    #: «кто ИЛИ какое правило», та же форма, что у одобрения канона (§93):
    #: раздел эталонного справочника подтверждается правилом с версией,
    #: канон без кода — человеком.
    scope_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    scope_confirmed_rule = models.CharField(max_length=100, blank=True, default="")
    scope_rule_version = models.CharField(max_length=32, blank=True, default="")
    scope_confirmed_at = models.DateTimeField(null=True, blank=True)
    scope_source_ref = models.CharField(max_length=200, blank=True, default="")
    #: Версия канонического описания услуги. Обязательна, как только
    #: назначено семейство (CheckConstraint ниже): решение о безопасности,
    #: принятое по канону без версии, нельзя потом воспроизвести.
    canonical_version = models.CharField(max_length=32, blank=True, default="")
    #: Версия клинической политики, по которой оценивается услуга.
    #: Пусто — клиническая политика ещё не утверждена (D-2/D-4); не
    #: обязательна, чтобы канон можно было завести до клиники.
    clinical_policy_version = models.CharField(max_length=32, blank=True, default="")
    #: Версия политики владельца (что салону разрешено предлагать).
    owner_policy_version = models.CharField(max_length=32, blank=True, default="")
    #: Юридический статус. Семантика значений не утверждена — ждёт юриста
    #: РФ (D-1), поэтому ни словаря, ни умолчания: ``NULL`` значит
    #: «не определён», и читатель обязан понимать это как «не разрешено»
    #: (fail-closed), а не как «разрешено, потому что не запрещено».
    legal_status = models.CharField(max_length=32, null=True, blank=True)

    # -- Body Care §7A-0: юридический класс и требуемая квалификация --------
    # Контракт v0.2 §2.1 / §7A.1 / §7A.4. Только поля: значения процедурам
    # ставят юрист (класс, D-1) и клиника (квалификация, D-2). ``NULL`` и
    # ``LEGAL_REVIEW_REQUIRED`` — «не разрешено»: гейт читает это в §7A-6.
    legal_service_class = models.CharField(
        max_length=24,
        choices=LegalServiceClass.choices,
        null=True,
        blank=True,
        help_text="Юридический класс (§7A.1); пусто — не установлен, читается как «не разрешено»",
    )
    required_practitioner_class = models.CharField(
        max_length=24,
        choices=PractitionerClass.choices,
        null=True,
        blank=True,
        help_text="Требуемая квалификация исполнителя (§7A.4); пусто — не установлена",
    )
    # Оба значения — ПОДТВЕРЖДЁННЫЕ, и пишет их только человек (главное
    # окно, 06.10, по запрету владельца «класс без автора нельзя»):
    # заданное значение требует кто + когда + основание (CheckConstraint).
    # Варианта «правило» нет: системный вывод класса (§7A-1, «кандидат»)
    # живёт в отдельной паре и гейт не открывает. Класс (юрист, D-1) и
    # квалификация (клиника, D-2) — разные решения, у каждого свой
    # провенанс. Кто вправе подтверждать — слой прав, не база.
    legal_class_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    legal_class_confirmed_at = models.DateTimeField(null=True, blank=True)
    legal_class_source_ref = models.CharField(max_length=200, blank=True, default="")
    practitioner_class_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    practitioner_class_confirmed_at = models.DateTimeField(null=True, blank=True)
    practitioner_class_source_ref = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('category', 'name')]
        ordering = ['-is_popular', 'sort_order', 'name']
        indexes = [
            models.Index(fields=['category', 'is_popular', 'sort_order']),
            # Очередь одобрения выбирается по этому полю, и она же —
            # рабочий список куратора справочника.
            models.Index(fields=['lifecycle'], name='svctpl_lifecycle_idx'),
        ]
        constraints = [
            # MAP-AUTO-01: код уникален среди непустых; NULL — сколько угодно.
            models.UniqueConstraint(
                fields=["canonical_code"],
                condition=models.Q(canonical_code__isnull=False),
                name="servicetemplate_canonical_code_uniq",
            ),
            # Одобрение — решение, и провенанс ему нужен по той же
            # причине, что и подтверждению связи: без автора оно через
            # месяц неотличимо от умолчания. Форма условия намеренно
            # повторяет `salonservice_verified_requires_provenance`.
            models.CheckConstraint(
                condition=(
                    ~models.Q(lifecycle="approved")
                    | (
                        models.Q(approved_at__isnull=False)
                        & ~models.Q(approval_source_ref="")
                        & (
                            models.Q(approved_by__isnull=False)
                            | ~models.Q(approved_rule="")
                        )
                    )
                ),
                name="servicetemplate_approved_requires_provenance",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(approved_by__isnull=False)
                    & ~models.Q(approved_rule="")
                ),
                name="servicetemplate_approval_is_who_xor_rule",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(approved_rule="")
                    | ~models.Q(approval_rule_version="")
                ),
                name="servicetemplate_approval_rule_carries_version",
            ),
            # DRF-2614: подтверждение ФЛАГА здоровья — то же устройство, что
            # у одобрения строки выше: провенанс обязателен, «кто ИЛИ
            # правило», правило несёт версию.
            models.CheckConstraint(
                condition=(
                    ~models.Q(health_check_origin="confirmed")
                    | (
                        models.Q(health_check_confirmed_at__isnull=False)
                        & ~models.Q(health_check_source_ref="")
                        & (
                            models.Q(health_check_confirmed_by__isnull=False)
                            | ~models.Q(health_check_confirmed_rule="")
                        )
                    )
                ),
                name="servicetemplate_health_check_confirmed_requires_provenance",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(health_check_confirmed_by__isnull=False)
                    & ~models.Q(health_check_confirmed_rule="")
                ),
                name="servicetemplate_health_check_is_who_xor_rule",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(health_check_confirmed_rule="")
                    | ~models.Q(health_check_rule_version="")
                ),
                name="servicetemplate_health_check_rule_carries_version",
            ),
            # Body Care CAT-2: вывод из оборота — решение того же веса, что
            # одобрение, и провенанс у него тот же.
            models.CheckConstraint(
                condition=(
                    ~models.Q(lifecycle="retired")
                    | (
                        models.Q(retired_at__isnull=False)
                        & ~models.Q(retirement_source_ref="")
                        & (
                            models.Q(retired_by__isnull=False)
                            | ~models.Q(retired_rule="")
                        )
                    )
                ),
                name="servicetemplate_retired_requires_provenance",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(retired_by__isnull=False)
                    & ~models.Q(retired_rule="")
                ),
                name="servicetemplate_retirement_is_who_xor_rule",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(retired_rule="")
                    | ~models.Q(retirement_rule_version="")
                ),
                name="servicetemplate_retirement_rule_carries_version",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    lifecycle__in=["provisional", "candidate", "approved", "retired"]
                ),
                name="servicetemplate_lifecycle_known",
            ),
            # Body Care CAT-1: назначенное семейство без версии канона —
            # решение, которое нельзя воспроизвести. Услуга вне body-care
            # (семейство NULL) версии не требует.
            models.CheckConstraint(
                condition=(
                    models.Q(service_family__isnull=True)
                    | ~models.Q(canonical_version="")
                ),
                name="servicetemplate_family_requires_canonical_version",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(service_family__isnull=True)
                    | models.Q(
                        service_family__in=[
                            "body_wrap", "spa_body", "mechanical_scrub", "acid_care",
                        ]
                    )
                ),
                name="servicetemplate_service_family_known",
            ),
            # Область классификации: словарь, согласованность с семейством
            # и провенанс. Семейство утверждает «подлежит Body Care», поэтому
            # семейство при неизвестной области или при ``not_body_care`` —
            # противоречие. Обратное не требуется: ``body_care`` без
            # семейства — «подлежит, семейство не определено».
            models.CheckConstraint(
                condition=(
                    models.Q(body_care_scope__isnull=True)
                    | models.Q(body_care_scope__in=["body_care", "not_body_care"])
                ),
                name="servicetemplate_body_care_scope_known",
            ),
            models.CheckConstraint(
                # ``IS NOT NULL`` назван явно: сравнение NULL с 'body_care'
                # даёт NULL, а CHECK отклоняет только FALSE — без него
                # семейство при неизвестной области прошло бы.
                condition=(
                    models.Q(service_family__isnull=True)
                    | (
                        models.Q(body_care_scope__isnull=False)
                        & models.Q(body_care_scope="body_care")
                    )
                ),
                name="servicetemplate_family_requires_body_care_scope",
            ),
            # Заданная область требует: кто ИЛИ правило (не оба и не ни
            # одного), когда и основание; правило — с версией.
            models.CheckConstraint(
                condition=(
                    models.Q(body_care_scope__isnull=True)
                    | (
                        models.Q(scope_confirmed_at__isnull=False)
                        & ~models.Q(scope_source_ref="")
                        & (
                            (
                                models.Q(scope_confirmed_by__isnull=False)
                                & models.Q(scope_confirmed_rule="")
                                & models.Q(scope_rule_version="")
                            )
                            | (
                                models.Q(scope_confirmed_by__isnull=True)
                                & ~models.Q(scope_confirmed_rule="")
                                & ~models.Q(scope_rule_version="")
                            )
                        )
                    )
                ),
                name="servicetemplate_body_care_scope_requires_provenance",
            ),
            # Body Care §7A-0: мимо ORM в базу не попадает класс или
            # квалификация вне словаря контракта.
            models.CheckConstraint(
                condition=(
                    models.Q(legal_service_class__isnull=True)
                    | models.Q(
                        legal_service_class__in=[
                            "non_medical_cosmetic", "medical_cosmetology",
                            "medical_other", "legal_review_required",
                        ]
                    )
                ),
                name="servicetemplate_legal_service_class_known",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(required_practitioner_class__isnull=True)
                    | models.Q(
                        required_practitioner_class__in=[
                            "cosmetic_esthetician", "nurse_cosmetology",
                            "physician_cosmetologist", "medical_specialist",
                            "protocol_specific",
                        ]
                    )
                ),
                name="servicetemplate_practitioner_class_known",
            ),
            # §7A-0: класс и квалификация без автора, даты и основания в базу
            # не попадают — ни через ORM, ни через ``update()``.
            models.CheckConstraint(
                condition=(
                    models.Q(legal_service_class__isnull=True)
                    | (
                        models.Q(legal_class_confirmed_by__isnull=False)
                        & models.Q(legal_class_confirmed_at__isnull=False)
                        & ~models.Q(legal_class_source_ref="")
                    )
                ),
                name="servicetemplate_legal_class_requires_confirmation",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(required_practitioner_class__isnull=True)
                    | (
                        models.Q(practitioner_class_confirmed_by__isnull=False)
                        & models.Q(practitioner_class_confirmed_at__isnull=False)
                        & ~models.Q(practitioner_class_source_ref="")
                    )
                ),
                name="servicetemplate_practitioner_class_requires_confirmation",
            ),
        ]

    def _assert_canonical_code_immutable(self) -> None:
        """Непустой код не меняется: смена кода — другая строка справочника.

        Сравнение с базой, а не с `__init__`-снимком: снимок обходится
        `refresh_from_db()`/повторным присваиванием, база — нет. Пустой
        код заполнить можно (bootstrap 0024, MAP-AUTO-03), снять или
        заменить — нельзя.
        """
        if self._state.adding:
            return
        stored = (
            type(self)._default_manager.filter(pk=self.pk)
            .values_list("canonical_code", flat=True).first()
        )
        if stored and (self.canonical_code or None) != stored:
            raise ValidationError({
                "canonical_code": (
                    f"canonical_code неизменяем: стоит {stored}, попытка записать "
                    f"{self.canonical_code!r}. Другой код — другая строка справочника."
                ),
            })

    #: Правило, которым область ставится канону с назначенным семейством.
    #: То же, что в миграции ``0048`` (расхождение сторожит узел).
    SCOPE_BY_FAMILY_RULE = "family_implies_body_care"
    SCOPE_BY_FAMILY_RULE_VERSION = "1"

    def _scope_follows_family(self, save_kwargs: dict) -> None:
        """Назначенное семейство утверждает область — записать её правилом.

        Только когда область ещё неизвестна: поставленную человеком область
        правило не переписывает, а противоречие (семейство при
        ``not_body_care``) оставляет базе — она его отклонит. ``update()``
        сюда не заходит: там область обязан назвать вызывающий.
        """
        if not self.service_family or self.body_care_scope is not None:
            return
        self.body_care_scope = self.BodyCareScope.BODY_CARE
        self.scope_confirmed_by = None
        self.scope_confirmed_rule = self.SCOPE_BY_FAMILY_RULE
        self.scope_rule_version = self.SCOPE_BY_FAMILY_RULE_VERSION
        self.scope_confirmed_at = timezone.now()
        self.scope_source_ref = "семейство Body Care назначено каноном"
        update_fields = save_kwargs.get("update_fields")
        if update_fields is not None:
            save_kwargs["update_fields"] = [
                *update_fields,
                "body_care_scope", "scope_confirmed_by", "scope_confirmed_rule",
                "scope_rule_version", "scope_confirmed_at", "scope_source_ref",
            ]

    def clean(self) -> None:
        self._assert_canonical_code_immutable()
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

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Неизменяемость — не только для формы: ORM-запись тоже её держит.
        # `update()` она не ловит — это известный предел, как у всех
        # правил уровня модели; сторож на схеме — уникальность.
        self._assert_canonical_code_immutable()
        if self.canonical_code == "":
            self.canonical_code = None
        self._scope_follows_family(kwargs)
        changes = self._significant_changes()
        adding = self._state.adding
        # DRF-2741: откуда процедура уходит. Противопоказание, названное
        # прежней категорией, теряет её из области — читать прежнюю категорию
        # нужно до записи.
        left_category_id = (
            type(self).objects.filter(pk=self.pk).values_list("category_id", flat=True).first()
            if any(change["field"] == "category" for change in changes) else None
        )
        # Запись и сброс — одной транзакцией: иначе при сбое сброса правка
        # уже лежит в базе, повторное сохранение разницы не увидит, и
        # подтверждения переживут правку.
        with transaction.atomic():
            super().save(*args, **kwargs)
            #: Что это сохранение сделало со знанием о процедуре — читает
            #: админка, чтобы сказать куратору.
            self.knowledge_reset = (
                reset_claims_of_procedure(
                    self, reason=ClaimApprovalReset.Reason.PROCEDURE_CHANGED, changes=changes,
                )
                if changes else None
            )
            if left_category_id is not None:
                self.knowledge_reset["contraindications"] += reset_contraindications(
                    contraindications_in_scope(category_ids=[left_category_id]),
                    reason=ClaimApprovalReset.Reason.PROCEDURE_CHANGED, changes=changes,
                )
            if adding:
                # DRF-2741: новая процедура молча попала бы под противопоказание,
                # область которого названа её категорией, — а рецензент проверял
                # правило для прежнего состава.
                self.knowledge_reset = {
                    "capabilities": 0, "goal_links": 0,
                    "contraindications": reset_contraindications(
                        contraindications_in_scope(category_ids=[self.category_id]),
                        reason=ClaimApprovalReset.Reason.SCOPE_CHANGED,
                        changes=[{"field": "templates", "old": "", "new": self.name}],
                    ),
                }

    #: Поля процедуры, правка которых заведомо НЕ меняет того, о чём утверждает
    #: знание о ней (DRF-2726, требование владельца): штамп версии каталога
    #: (каким изданием справочника и когда строка одобрена) и витринные
    #: признаки. Смена номера версии каталога сама по себе подтверждения не
    #: снимает. Список разрешительный: любое другое поле — в том числе
    #: добавленное позже — считается значимым, потому что неизменность смысла
    #: по нему доказать нечем. В списке НЕТ ``lifecycle`` и отметок о
    #: происхождении флага гейта здоровья (``health_check_*``): одобрение
    #: чернового канона и подтверждение флага здоровья меняют то, что известно
    #: о процедуре, — и знание о ней подтверждается заново.
    KNOWLEDGE_NEUTRAL_FIELDS = frozenset({
        "approval_rule_version", "approval_source_ref", "approved_at", "approved_by", "approved_rule",
        "is_popular", "sort_order", "created_at", "updated_at",
    })

    def _significant_changes(self) -> list[dict[str, str]]:
        """Какие значимые для знания поля меняет это сохранение: ``[{field, old, new}]``."""
        if self._state.adding:
            return []
        fields = [
            f for f in self._meta.concrete_fields
            if f.name not in self.KNOWLEDGE_NEUTRAL_FIELDS and not f.primary_key
        ]
        stored = type(self).objects.filter(pk=self.pk).values(*(f.attname for f in fields)).first()
        if stored is None:
            return []
        changes = []
        for f in fields:
            before, after = stored[f.attname], getattr(self, f.attname)
            if before == after:
                continue
            if _same_text(before, after):
                # Перевод строки из браузера (CRLF) против засеянного (LF) —
                # не правка: иначе первое же сохранение формы сбрасывало бы знание.
                continue
            if f.name == "category":
                names = dict(ServiceCategory.objects.filter(pk__in=[before, after]).values_list("pk", "name"))
                before, after = names.get(before, before), names.get(after, after)
            changes.append({"field": f.name, "old": _journal_text(before), "new": _journal_text(after)})
        return changes

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
    ставит человек либо **названное правило** — с именем, версией и датой
    (§77 п.30 от 24.09: выбор мастера из канона, DRF-2406). Что именно
    подтверждает синоним, от этого не изменилось: он по-прежнему ничего не
    подтверждает, потому что совпадение строк не является действием.

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
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
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
    image = MetadataFreeImageField(
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
    # resolved_duration below, and SpecialistService.resolved_duration,
    # which puts the specialist's own override in front of it).
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
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    mapping_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
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

    #: Body Care CAT-3 (контракт v0.1 §3): версия конфигурации, к которой
    #: относятся факты :class:`OfferingConfigFact` этого предложения. Пусто —
    #: конфигурация не описана; ни один факт ещё не утверждён.
    configuration_version = models.CharField(max_length=32, blank=True, default="")

    # -- Ревью конфигурации (Body Care CAT-6, контракт §7) -------------------
    # §7: REVIEW_REQUIRED — «факты есть, но требуется policy/legal/protocol
    # review». Если бы полнота фактов сама давала READY_FOR_SCREENING, этого
    # ревью не было бы вовсе. Поэтому READY — только при ЯВНОМ ревью, и оно
    # привязано к версии конфигурации и к отпечатку её фактов: изменили
    # конфигурацию — ревью устарело, предложение снова REVIEW_REQUIRED. Хранится решение, а не
    # состояние: состояние вычисляет ``services.body_care_validation``.
    # Форма провенанса — «кто ИЛИ правило», как у ``approved_*``.
    config_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    config_review_rule = models.CharField(max_length=100, blank=True, default="")
    config_review_rule_version = models.CharField(max_length=32, blank=True, default="")
    config_reviewed_at = models.DateTimeField(null=True, blank=True)
    config_review_source_ref = models.CharField(max_length=200, blank=True, default="")
    #: Какую версию конфигурации проверили. Ревью действует, только пока
    #: она равна ``configuration_version``.
    config_reviewed_version = models.CharField(max_length=32, blank=True, default="")
    #: Отпечаток фактов конфигурации, которые видел ревьюер (SHA-256, см.
    #: ``body_care_validation.config_fingerprint``). Ревью действует, только
    #: пока отпечаток текущих фактов с ним совпадает: любая правка факта —
    #: значение, состояние, источник, удаление строки — делает ревью
    #: неактуальным без ручного подъёма версии (решение главного окна, 06.10).
    config_reviewed_fingerprint = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Body Care CAT-6: ревью конфигурации — решение, и провенанс у него
            # тот же, что у подтверждения связи: дата, основание, кто ИЛИ правило.
            models.CheckConstraint(
                condition=(
                    models.Q(config_reviewed_version="")
                    | (
                        models.Q(config_reviewed_at__isnull=False)
                        & ~models.Q(config_review_source_ref="")
                        & ~models.Q(config_reviewed_fingerprint="")
                        & (
                            models.Q(config_reviewed_by__isnull=False)
                            | ~models.Q(config_review_rule="")
                        )
                    )
                ),
                name="salonservice_config_review_requires_provenance",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(config_reviewed_by__isnull=False)
                    & ~models.Q(config_review_rule="")
                ),
                name="salonservice_config_review_is_who_xor_rule",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(config_review_rule="")
                    | ~models.Q(config_review_rule_version="")
                ),
                name="salonservice_config_review_rule_carries_version",
            ),
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
            # «Подтверждена» — связь с ЧЕМ? `VERIFIED` без `template` — это
            # подтверждение пустоты (DRF-1668, план automapping C-9): до
            # этого ограничения повторный confirm YClients-драфта
            # переписывал `template` (и в NULL) у решённой строки,
            # `mapping_status` не трогая, — разметка владельца стиралась
            # без следа, а гейт подбора продолжал читать «проверено».
            #
            # Только `verified`: `not_recommendable` — «решено, что связи
            # НЕ БУДЕТ», у него шаблона может и не быть по смыслу.
            models.CheckConstraint(
                condition=(
                    ~models.Q(mapping_status="verified")
                    | models.Q(template__isnull=False)
                ),
                name="salonservice_verified_requires_template",
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

    #: Поля подтверждения связи: что именно снимается, когда подтверждение
    #: перестаёт относиться к канону услуги (DRF-2883).
    _MAPPING_CONFIRMATION_FIELDS = (
        "mapping_confirmed_by_id", "mapping_confirmed_rule",
        "mapping_rule_version", "mapping_confirmed_at",
    )

    def canon_changed_under_a_standing_confirmation(self) -> bool:
        """Канон сменили, а подтверждение связи осталось прежним (DRF-2883).

        Связь подтверждают для конкретного канона. Сменить канон и оставить
        статус, автора и дату — значит рекомендовать услугу как канон Б по
        подтверждению, выданному для канона А. Тот же род, что область,
        заданная связью: смысл подтверждения определяется ссылкой, а ссылку
        можно сменить, не трогая само подтверждение.

        ``False``, если в этом же сохранении пришло новое подтверждение —
        другая дата, другой автор или другое правило: тот, кто сменил канон,
        подтвердил связь заново.
        """
        if self._state.adding or self.mapping_status != self.MappingStatus.VERIFIED:
            return False
        stored = (
            type(self).objects.filter(pk=self.pk)
            .values("template_id", "mapping_status", *self._MAPPING_CONFIRMATION_FIELDS)
            .first()
        )
        if stored is None or stored["mapping_status"] != self.MappingStatus.VERIFIED:
            return False
        if stored["template_id"] == self.template_id:
            return False
        return all(
            self._same_confirmation_value(stored[name], getattr(self, name))
            for name in self._MAPPING_CONFIRMATION_FIELDS
        )

    @staticmethod
    def _same_confirmation_value(stored: object, given: object) -> bool:
        """Дата сравнивается до секунды: форма админки возвращает её без
        микросекунд, и усечённая прежняя дата не должна сойти за новое
        подтверждение."""
        if hasattr(stored, "microsecond") and hasattr(given, "microsecond"):
            return stored.replace(microsecond=0) == given.replace(microsecond=0)
        return stored == given

    def _drop_a_confirmation_given_for_another_canon(self, save_kwargs: dict) -> None:
        """Подтверждение, выданное для прежнего канона, не переносится на новый.

        Связь возвращается в очередь проверки: ``review_required`` — «связь
        есть, но происхождения недостаточно». Автор, правило и дата
        снимаются; основание (``mapping_source_ref``) остаётся как след того,
        откуда связь взялась, — подтверждением оно не служит.

        ``QuerySet.update()`` сюда не заходит — известный предел правил
        уровня модели.
        """
        if not self.canon_changed_under_a_standing_confirmation():
            return
        self.mapping_status = self.MappingStatus.REVIEW_REQUIRED
        self.mapping_confirmed_by = None
        self.mapping_confirmed_rule = ""
        self.mapping_rule_version = ""
        self.mapping_confirmed_at = None
        update_fields = save_kwargs.get("update_fields")
        if update_fields is not None:
            save_kwargs["update_fields"] = [
                *update_fields,
                "mapping_status", "mapping_confirmed_by", "mapping_confirmed_rule",
                "mapping_rule_version", "mapping_confirmed_at",
            ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        self._drop_a_confirmation_given_for_another_canon(kwargs)
        super().save(*args, **kwargs)

    def resolved_duration(self) -> int | None:
        """First non-null of salon -> template duration (DRF-2705).

        The salon-level half of :meth:`SpecialistService.resolved_duration`,
        which delegates here. ``None`` means nothing resolves: no salon value
        and either no template or a template whose timing is not curated yet.
        """
        if self.duration_minutes is not None:
            return self.duration_minutes
        template = self.template
        if template is not None:
            return template.duration_default
        return None

    def config_fact(self, field: str) -> tuple[str, object]:
        """Состояние и значение факта конфигурации — ``(state, value)``.

        Body Care CAT-3, контракт §4: «UNKNOWN не преобразуется в
        разрешение». Поэтому **отсутствие строки читается как
        ``UNKNOWN``**, а не как «ограничений нет»: салон, ничего не
        сказавший про время экспозиции, не сказал «любое».
        Неизвестное имя поля — ошибка вызывающего, а не ``UNKNOWN``:
        опечатка в имени иначе молча читалась бы как неизвестный факт.
        """
        if field not in OfferingConfigFact.Field.values:
            raise ValueError(f"unknown configuration field: {field!r}")
        fact = self.config_facts.filter(field=field).first()
        if fact is None:
            return OfferingConfigFact.State.UNKNOWN, None
        return fact.state, fact.value

    def __str__(self) -> str:
        return f"{self.name} @ {self.tenant.slug}"


class OfferingConfigFact(models.Model):
    """Факт конфигурации предложения салона — Body Care CAT-3 (контракт §3.1, §4).

    Решение владельца D-3 (06.10): ``SalonService`` и есть ``SalonOffering``
    контракта; факты конфигурации — отдельная таблица на него, по строке на
    поле. Почему не столбцы: полей девятнадцать, у каждого пять состояний и
    свой источник, и ``NULL`` в столбце не различил бы «неизвестно» от «не
    применимо» — контракт §4 прямо запрещает ``null`` как единственный смысл.

    ### Пять состояний (§4)

    ``KNOWN``           значение есть и у него есть источник (``source_ref``)
    ``UNKNOWN``         не знаем; значения нет — и это НЕ разрешение
    ``NOT_APPLICABLE``  для этой услуги поле не имеет смысла
    ``NOT_PROVIDED``    салон спросили — салон не ответил
    ``CONFLICT``        источники расходятся; расходящиеся варианты могут
                        лежать в ``value``

    Нет строки — то же, что ``UNKNOWN`` (:meth:`SalonService.config_fact`).

    Полный провенанс факта (``source_type``, ``source_version``,
    ``captured_at/by``, ``confidence`` — контракт §5) — это CAT-4; здесь
    только ``source_ref``, без которого ``KNOWN`` не принимается вовсе.
    """

    class Field(models.TextChoices):
        """Поля конфигурации — ровно список контракта §3.1, закрытый."""

        PRODUCT_NAME = "product_name", "Продукт"
        PRODUCT_ARTICLE = "product_article", "Артикул"
        MANUFACTURER = "manufacturer", "Производитель"
        INSTRUCTION_VERSION = "instruction_version", "Версия инструкции"
        INSTRUCTION_REGION = "instruction_region", "Регион инструкции"
        APPLICATION_AREA = "application_area", "Зона нанесения"
        APPLICATION_AREA_SIZE = "application_area_size", "Размер зоны"
        MODE = "mode", "Режим"
        EXPOSURE_SECONDS = "exposure_seconds", "Время экспозиции, с"
        APPLICATION_COUNT = "application_count", "Число нанесений"
        COVERING_TYPE = "covering_type", "Тип укрытия"
        REMOVAL_METHOD = "removal_method", "Способ снятия"
        AFTERCARE = "aftercare", "Уход после"
        ADDITIONAL_MODALITY = "additional_modality", "Дополнительная модальность"
        HEAT_MODE = "heat_mode", "Тепловой режим"
        COLD_MODE = "cold_mode", "Холодовой режим"
        COMPRESSION_MODE = "compression_mode", "Компрессия"
        DEVICE_REFERENCE = "device_reference", "Аппарат"
        PROTOCOL_SOURCE = "protocol_source", "Источник протокола"

    class State(models.TextChoices):
        KNOWN = "known", "Известно"
        UNKNOWN = "unknown", "Неизвестно"
        NOT_APPLICABLE = "not_applicable", "Не применимо"
        NOT_PROVIDED = "not_provided", "Не предоставлено"
        CONFLICT = "conflict", "Противоречие"

    class SourceType(models.TextChoices):
        """Откуда взят факт — контракт §5, ровно семь значений (CAT-4).

        ``SYSTEM_DERIVED`` в словаре есть, но §5 запрещает его для
        клинического факта, которого нет в политике. Какие из 19 полей
        «клинические», контракт не перечисляет (вопрос владельцу Q2) —
        поэтому запрет здесь не реализован, а не угадан.
        """

        MANUFACTURER_INSTRUCTION = "manufacturer_instruction", "Инструкция производителя"
        PROTOCOL_DOCUMENT = "protocol_document", "Документ протокола"
        OWNER_INPUT = "owner_input", "Ввод владельца"
        SALON_INPUT = "salon_input", "Ввод салона"
        PHYSICIAN_POLICY = "physician_policy", "Политика врача"
        CANONICAL_POLICY = "canonical_policy", "Каноническая политика"
        SYSTEM_DERIVED = "system_derived", "Выведено системой"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    salon_service = models.ForeignKey(
        SalonService,
        on_delete=models.CASCADE,
        related_name="config_facts",
    )
    field = models.CharField(max_length=32, choices=Field.choices)
    state = models.CharField(max_length=16, choices=State.choices)
    #: Значение факта. Тип у полей разный (секунды, строки, ссылки), поэтому
    #: JSON. Пусто у UNKNOWN / NOT_APPLICABLE / NOT_PROVIDED — CheckConstraint.
    value = models.JSONField(null=True, blank=True)
    #: Откуда известно значение. Обязательно у KNOWN: известный факт без
    #: источника через месяц неотличим от догадки (контракт §5).
    source_ref = models.CharField(max_length=200, blank=True, default="")
    # -- Провенанс факта (контракт §5, Body Care CAT-4) ---------------------
    #: Вид источника. NULL законен у факта, который ещё не известен
    #: (UNKNOWN / NOT_PROVIDED / NOT_APPLICABLE): провенанса у отсутствия нет,
    #: и пустой вид источника там не читается как «проверено». У KNOWN —
    #: обязателен (CheckConstraint ниже).
    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        null=True,
        blank=True,
    )
    #: Версия источника — например, редакция инструкции производителя.
    source_version = models.CharField(max_length=64, blank=True, default="")
    #: Когда факт зафиксирован (не путать с датой самого источника).
    captured_at = models.DateTimeField(null=True, blank=True)
    #: Кто зафиксировал факт — «кто ИЛИ правило», та же форма, что у
    #: ``approved_by``/``approved_rule`` и ``retired_by``/``retired_rule``:
    #: ручной ввод салона или владельца — человек, загрузка из инструкции
    #: или производное — правило с версией. PROTECT: провенанс не должен
    #: молча исчезать при удалении учётки (в переписи удаления — RETAIN).
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    captured_rule = models.CharField(max_length=100, blank=True, default="")
    capture_rule_version = models.CharField(max_length=32, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Один факт на поле у предложения: два «известных» значения одного
            # поля — это CONFLICT, и он выражается состоянием, а не дублем.
            models.UniqueConstraint(
                fields=["salon_service", "field"],
                name="offeringconfigfact_one_fact_per_field",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    field__in=[
                        "product_name", "product_article", "manufacturer",
                        "instruction_version", "instruction_region",
                        "application_area", "application_area_size", "mode",
                        "exposure_seconds", "application_count", "covering_type",
                        "removal_method", "aftercare", "additional_modality",
                        "heat_mode", "cold_mode", "compression_mode",
                        "device_reference", "protocol_source",
                    ]
                ),
                name="offeringconfigfact_field_known",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "known", "unknown", "not_applicable", "not_provided", "conflict",
                    ]
                ),
                name="offeringconfigfact_state_known",
            ),
            # §4: KNOWN — это значение с источником.
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="known")
                    | (models.Q(value__isnull=False) & ~models.Q(source_ref=""))
                ),
                name="offeringconfigfact_known_requires_value_and_source",
            ),
            # CAT-4, §5: известный факт несёт и ВИД источника, а не только
            # ссылку на него — иначе «инструкция производителя» и «так сказал
            # салон» неразличимы для того, кто решает о безопасности.
            models.CheckConstraint(
                condition=~models.Q(state="known") | models.Q(source_type__isnull=False),
                name="offeringconfigfact_known_requires_source_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_type__isnull=True)
                    | models.Q(
                        source_type__in=[
                            "manufacturer_instruction", "protocol_document", "owner_input",
                            "salon_input", "physician_policy", "canonical_policy",
                            "system_derived",
                        ]
                    )
                ),
                name="offeringconfigfact_source_type_known",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(captured_by__isnull=False) & ~models.Q(captured_rule="")
                ),
                name="offeringconfigfact_capture_is_who_xor_rule",
            ),
            models.CheckConstraint(
                condition=models.Q(captured_rule="") | ~models.Q(capture_rule_version=""),
                name="offeringconfigfact_capture_rule_carries_version",
            ),
            # §4: «не знаем», «не применимо», «не ответили» значения не несут —
            # иначе значение при UNKNOWN однажды прочтут как известное.
            models.CheckConstraint(
                condition=(
                    ~models.Q(state__in=["unknown", "not_applicable", "not_provided"])
                    | models.Q(value__isnull=True)
                ),
                name="offeringconfigfact_absent_states_carry_no_value",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.field}={self.state} @ {self.salon_service_id}"


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
        return self.salon_service.resolved_duration()

    def resolved_requires_health_check(self) -> bool | None:
        """Вердикт гейта — см. :meth:`resolved_health_check` (DRF-2614)."""
        return self.resolved_health_check()[0]

    def resolved_health_check(self) -> tuple[bool | None, str]:
        """Вердикт гейта и ЕГО ОСНОВАНИЕ — одним расчётом (DRF-2614).

        Основание — одно из :data:`HEALTH_CHECK_BASES`: чьим словом решён
        вердикт. Два вердикта ``True`` с разным основанием — разные
        состояния: ``template_inferred`` — черновой пол, выведенный
        правилом, ``template_confirmed`` — просмотренный. Решение владельца
        §95 («черновой гейт не требует скрининга») различает именно их;
        сам вердикт здесь не меняется, пока владелец не скажет.

        Escalate-only OR across template floor, salon, specialist (D1).

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
        template_basis = (
            "template_confirmed"
            if template is not None
            and template.health_check_origin == ServiceTemplate.HealthCheckOrigin.CONFIRMED
            else "template_inferred"
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
        #    DRF-2614: провенанс флага появился (`health_check_origin`), и
        #    черновой пол теперь ОТЛИЧИМ — основание `template_inferred`
        #    против `template_confirmed`. Сам вердикт этот лист не меняет:
        #    перестать требовать скрининг по черновому полу (§95) — слово
        #    владельца, не побочный эффект разметки.
        if template_floor is True:
            return True, template_basis
        # 2. Эскалация мастера. Он вправе поднять и не вправе опустить,
        #    поэтому поле остаётся двузначным — см. его докстринг.
        if self.requires_health_check:
            return True, "specialist"
        # 3. Ответ салона, если салон отвечал. Трёхзначное поле: `False`
        #    здесь — это сказанное «нет», а не молчание.
        if salon.requires_health_check is not None:
            return bool(salon.requires_health_check), "salon"
        # 4. Шаблон есть и флага не несёт — ответил канон.
        if template_floor is False:
            return False, template_basis
        # 5. Опоры нет и никто не отвечал. Отсутствие свидетельства не
        #    является свидетельством безопасности.
        return None, "unknown"

    def clean(self) -> None:
        if self.is_active and self.resolved_duration() is None:
            raise ValidationError(
                {"duration_minutes": "An active bookable service needs a resolvable duration."}
            )
        self._clean_same_tenant()

    def _clean_same_tenant(self) -> None:
        """Мастер и услуга ребра — из одного салона (решение владельца, раздел Q).

        Салоны читаются из базы, а не с закешированных объектов: вызывающий
        мог держать экземпляр профиля до переезда мастера, и решать по нему
        значило бы решать по прошлому.

        Ключи ошибок выбраны под формы. ``specialist`` есть и в салонном
        инлайне, и в отдельной форме ребра. Расхождение собственного тенанта
        ребра — ошибка без поля: у инлайна поля ``tenant`` нет, а ``ModelForm``
        превращает ошибку модели на отсутствующем поле в ``ValueError``, то есть в 500.

        Названный предел, как у ``SpecialistProfile.clean`` для ``works_at``:
        ``bulk_create`` и ``update()`` идут мимо ``clean()``, а ``CheckConstraint``
        в соседнюю таблицу не смотрит.
        """
        if self.salon_service_id is None or self.specialist_id is None:
            return
        specialist_model = self._meta.get_field("specialist").related_model
        master_tenant_id = (
            specialist_model.objects
            .filter(pk=self.specialist_id)
            .values_list("tenant_id", flat=True)
            .first()
        )
        salon_tenant_id = (
            SalonService.objects
            .filter(pk=self.salon_service_id)
            .values_list("tenant_id", flat=True)
            .first()
        )
        if master_tenant_id is None or master_tenant_id != salon_tenant_id:
            raise ValidationError({
                "specialist": (
                    "Мастер не из салона этой услуги. Услугу салона оказывает "
                    "только мастер этого салона."
                ),
            })
        if self.tenant_id is not None and self.tenant_id != salon_tenant_id:
            raise ValidationError(
                "Салон предложения не совпадает с салоном услуги — "
                "такую строку нужно исправить, а не сохранять."
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.tenant_id is None and self.salon_service_id is not None:
            self.tenant_id = self.salon_service.tenant_id
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.salon_service.name} — {self.specialist.display_name}"


class CanonGapRequest(models.Model):
    """Заявка мастера о разрыве канона — «своя услуга» (G6 / D6, M9, DRF-1801).

    Принцип владельца (DRF-1349 08:31): канон первичен, «своя услуга» — не
    новая каноническая строка и не предложение мастера, а заявка к
    владельцу. ``PENDING`` ничего не создаёт в каноне; решает только
    человек в Django-admin (``services.canon_gap.decide``, §143 — актор
    обязателен). Смысл статусов и пути — в докстринге ``services/canon_gap.py``.

    Инварианты — в схеме, а не в договорённости: ``clean()`` обходится
    любым ``update()``, а решение без автора через месяц читается как
    умолчание.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "На проверке"
        APPROVED = "approved", "Подтверждена"
        NEEDS_CLARIFICATION = "needs_clarification", "Нужно уточнение"
        REJECTED = "rejected", "Отклонена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Салон мастера на момент заявки; у соло-мастера без салона — NULL.
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.PROTECT, null=True, blank=True,
        related_name="canon_gap_requests",
    )
    specialist = models.ForeignKey(
        "users.SpecialistProfile", on_delete=models.CASCADE,
        related_name="canon_gap_requests",
    )
    # Данные заявки — как мастер их ввёл (макет 3.2): это не «предположение
    # системы», а то, что владелец читает, решая.
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    duration_minutes = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    resolved_template = models.ForeignKey(
        ServiceTemplate, on_delete=models.PROTECT, null=True, blank=True,
        related_name="canon_gap_requests",
    )
    clarification_question = models.TextField(blank=True, default="")
    rejection_reason = models.TextField(blank=True, default="")
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Заявка о разрыве канона"
        verbose_name_plural = "Заявки о разрыве канона"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["specialist", "status"], name="canongap_specialist_status_idx"),
            models.Index(fields=["status", "created_at"], name="canongap_status_created_idx"),
        ]
        constraints = [
            # «Подтверждена» — связь С ЧЕМ: без шаблона подтверждения нет.
            models.CheckConstraint(
                condition=~models.Q(status="approved") | models.Q(resolved_template__isnull=False),
                name="canongap_approved_requires_template",
            ),
            # До решения канон не называется: PENDING шаблона не несёт.
            models.CheckConstraint(
                condition=~models.Q(status="pending") | models.Q(resolved_template__isnull=True),
                name="canongap_pending_binds_no_template",
            ),
            # Решение — всегда с автором и временем (§143).
            models.CheckConstraint(
                condition=(
                    models.Q(status="pending")
                    | (models.Q(decided_by__isnull=False) & models.Q(decided_at__isnull=False))
                ),
                name="canongap_decision_requires_actor",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="needs_clarification") | ~models.Q(clarification_question=""),
                name="canongap_clarification_requires_question",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="rejected") | ~models.Q(rejection_reason=""),
                name="canongap_rejection_requires_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"


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

    def save(self, *args: Any, **kwargs: Any) -> None:
        # DRF-2726: связь возможности с целью утверждает «помогает ЭТОЙ цели».
        # Смена ключа или названия цели меняет предмет утверждения — её
        # подтверждённые связи возвращаются в черновик (адресно: только связи
        # этой цели).
        changes = []
        if not self._state.adding:
            stored = type(self).objects.filter(pk=self.pk).values("key", "label").first() or {}
            changes = [
                {"field": f"goal.{name}", "old": _journal_text(stored[name]), "new": _journal_text(getattr(self, name))}
                for name in ("key", "label")
                if name in stored and stored[name] != getattr(self, name)
            ]
        with transaction.atomic():
            super().save(*args, **kwargs)
            if changes:
                reset_claims(
                    capabilities=ProcedureCapability.objects.none(),
                    goal_links=CapabilityGoalLink.objects.filter(goal=self),
                    reason=ClaimApprovalReset.Reason.GOAL_CHANGED, changes=changes,
                )

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


class GoalDirection(models.Model):
    """Курируемое НАПРАВЛЕНИЕ под цель — WHAT карточки C04 (DRF-1772, К-3).

    Решение владельца §60/§61: «направление» — объект ответа C04, и его
    источник — данные владельца, как ``GoalOption`` (не модель, не имя
    услуги). Ключ — цель × область из анкеты (``area``); строка без области
    — запасная под цель целиком. Таблица пустая → направления нет →
    карточки нет → C04.4 (механически, OD_C04 §2). Фразы не выдумываются
    кодом: они приходят от владельца миграцией данных.

    Здесь только WHAT и его подстрока. Услуга, мастер, цена, слот — не
    здесь и не в карточке (B2/B3: Recommendation = WHAT; execution — C05).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    goal_option = models.ForeignKey(
        GoalOption,
        on_delete=models.CASCADE,
        related_name='directions',
    )
    area_key = models.SlugField(
        max_length=64,
        blank=True,
        default='',
        help_text=(
            "Ключ ответа шага «area» анкеты (face/body/hair/hands/overall); "
            "пусто — запасное направление под цель целиком"
        ),
    )
    what = models.CharField(
        max_length=200,
        help_text="Направление одной фразой (макет C04.1: «Уменьшить утреннюю отёчность…»)",
    )
    subline = models.CharField(
        max_length=300,
        blank=True,
        default='',
        help_text="Подстрока под направлением (макет: «Сфокусируемся на этом — …»)",
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('goal_option', 'area_key')]
        ordering = ['goal_option', 'sort_order', 'area_key']

    def __str__(self) -> str:
        scope = self.area_key or '*'
        return f"{self.goal_option.key} × {scope}: {self.what}"


# --- DRF-2606: возможность процедуры и её связь с целью ---------------------
#
# Решение владельца 29.09: «Capability описывает возможность процедуры, связь
# с целью — отдельной таблицей». Утверждение «процедура X помогает цели Y»
# распадается на два (§10 владельца — «не хранить монолитные обещания»), и у
# каждого своё основание, свой срок, свой статус claim.
#
# Здесь только НОСИТЕЛЬ и провенанс. Содержание собирает владелец; ни одной
# строки кодом не заполняется. Читателя на пути ответа нет — есть одна
# санкционированная функция чтения (:func:`client_facing_capabilities`),
# которая держит правило «вывод системы ≠ подтверждённое человеком».


def _journal_text(value: Any) -> str:
    """Значение поля — строкой для журнала снятых подтверждений."""
    return "" if value is None else str(value)


def _same_text(before: Any, after: Any) -> bool:
    """Два текста, различающиеся только видом перевода строки."""
    return (
        isinstance(before, str) and isinstance(after, str)
        and before.replace("\r\n", "\n") == after.replace("\r\n", "\n")
    )


def reset_claims(
    *, capabilities, goal_links, reason: str, changes: list[dict[str, str]], contraindications=None,
) -> dict[str, int]:
    """Снять подтверждение и проверку с ЭТИХ утверждений и записать, почему (DRF-2726).

    Требование владельца: подтверждение не переносится на изменившийся
    предмет, а куратор должен видеть, что и почему требует повторной
    проверки. По каждому затронутому утверждению пишется строка
    :class:`ClaimApprovalReset` — причина, что изменилось (было → стало),
    время; сам список затронутых и есть эти строки.

    Затронутое утверждение — то, которому есть что терять: подтверждённое
    или несущее отметку рецензента. Меняются только статус и отметка;
    содержание и след подтверждения (``confirmed_by``, ``confirmed_at``)
    остаются, как после миграций ``0032``–``0038``.

    В журнал приложения уходят числа и причина — без содержания.
    """
    counts: dict[str, int] = {}
    journal: list[ClaimApprovalReset] = []
    # Журнал и сброс — вместе или никак: строка в черновике без записи в
    # журнале не попала бы в очередь куратора.
    with transaction.atomic():
        groups = [("capabilities", capabilities, "capability"), ("goal_links", goal_links, "goal_link")]
        if contraindications is not None:
            # DRF-2741: правило безопасности, область которого изменилась, —
            # такое же знание о процедуре, и подтверждается заново.
            groups.append(("contraindications", contraindications, "contraindication"))
        for name, rows, kind in groups:
            affected = list(
                rows.filter(models.Q(status="approved") | models.Q(reviewed_by__isnull=False))
            )
            journal += [
                ClaimApprovalReset(
                    reason=reason, changes=changes, claim_kind=kind, claim_label=str(row)[:300],
                    was_approved=row.status == "approved", had_review=row.reviewed_by_id is not None,
                    **{kind: row},
                )
                for row in affected
            ]
            rows.model.objects.filter(pk__in=[row.pk for row in affected]).update(
                status="system_inference", reviewed_by=None, reviewed_at=None,
            )
            counts[name] = len(affected)
        ClaimApprovalReset.objects.bulk_create(journal)
    if journal:
        logger.warning("knowledge.approvals_reset reason=%s counts=%s", reason, counts)
    return counts


def reset_claims_of_procedure(template: Any, *, reason: str, changes: list[dict[str, str]]) -> dict[str, int]:
    """Снять подтверждение со знания об ЭТОЙ процедуре — и только о ней.

    Требование владельца: сброс адресный. Где система может определить,
    какие утверждения зависят от изменившегося, — сбрасываются только они
    (так устроены правка самого утверждения и правка возможности, от которой
    зависят её связи с целями). **От какого поля процедуры зависит конкретное
    утверждение, нигде не записано** — определить нечем, поэтому значимая
    правка процедуры возвращает в черновик ВСЕ утверждения этой процедуры.
    Знание о соседних процедурах не трогается никогда.

    Предел: срабатывает на ``save()`` процедуры и категории. Массовое
    ``QuerySet.update()`` его обходит — как любое правило уровня модели.
    """
    return reset_claims(
        capabilities=ProcedureCapability.objects.filter(template=template),
        goal_links=CapabilityGoalLink.objects.filter(capability__template=template),
        contraindications=contraindications_in_scope(template=template, category_ids=[template.category_id]),
        reason=reason, changes=changes,
    )


def contraindications_in_scope(*, template: Any = None, category_ids: Any = ()) -> Any:
    """Противопоказания, чья область применения задевает эту процедуру или эти категории (DRF-2741).

    Область строки названа процедурами и/или категориями. Категория покрывает
    свои процедуры, корневая — и процедуры своих подкатегорий (так же её
    читает :func:`services.knowledge_review.may_review_scope`), поэтому вместе
    с категорией берётся её корень.

    Правило, названное категорией, — знание о КАЖДОЙ её процедуре. Отсюда
    следствие, которое стоит знать куратору: значимая правка, появление,
    перенос или удаление любой процедуры категории возвращает такое правило в
    черновик. Правило, названное процедурами поимённо, соседние процедуры не
    задевают.
    """
    ids = [pk for pk in category_ids if pk is not None]
    scope = models.Q(categories__in=ids) | models.Q(
        categories__in=ServiceCategory.objects.filter(pk__in=ids).values("parent_id")
    )
    if template is not None:
        scope |= models.Q(templates=template)
    # Через ``pk__in``: строка с двумя совпавшими связями пришла бы дважды и
    # дважды попала бы в журнал.
    return ProcedureContraindication.objects.filter(
        pk__in=ProcedureContraindication.objects.filter(scope).values("pk")
    )


def reset_contraindications(rows: Any, *, reason: str, changes: list[dict[str, str]]) -> int:
    """Вернуть в черновик ЭТИ противопоказания; сколько строк затронуто."""
    return reset_claims(
        capabilities=ProcedureCapability.objects.none(), goal_links=CapabilityGoalLink.objects.none(),
        contraindications=rows, reason=reason, changes=changes,
    )["contraindications"]


#: Срок результата «без слов» — ни одной буквы, только цифры и знаки (DRF-2726).
#: Одно выражение на базу (ограничения ниже), на входы
#: (:func:`services.knowledge_intake.timeframe_errors`) и на шаг миграции.
TIMEFRAME_WITHOUT_WORDS = r"^[^A-Za-zА-Яа-яЁё]*$"


class ClaimEvidence(models.Model):
    """Основание утверждения — общее у возможности и у её связи с целью.

    Та же форма провенанса, что у связи услуги с каноном (§76) и у одобрения
    канона (§93): «кто ИЛИ какое правило», когда, по какому основанию. Поля
    наследуются как СВОИ КОЛОНКИ каждой таблицы: у связи с целью —
    собственные ``status``/``confirmed_by``/``source_ref``…, а не ссылка на
    основание возможности. «Подтверждённая возможность ≠ подтверждённая
    связь» (приёмка владельца 29.09, §6).

    Контракт статуса (приёмка владельца 29.09, §7) — четыре пункта:

    1. **Что измеряет.** Эпистемический статус ЗНАНИЯ — подтверждено ли
       утверждение о процедуре (или о её связи с целью) человеком или
       названным правилом, либо это вывод системы / черновик.
    2. **Чем отличается от** ``SalonService.mapping_status``. Тот измеряет
       СОПОСТАВЛЕНИЕ: «эта услуга салона — это вот этот канон»
       (UNMAPPED / REVIEW_REQUIRED / VERIFIED / NOT_RECOMMENDABLE). Это
       разные машины состояний: verified-сопоставление ничего не говорит о
       том, что процедура делает, а подтверждённое знание — о том, какой
       салон её продаёт. Слово «verified» здесь намеренно не используется.
    3. **Кто переводит.** Из ``system_inference`` в ``approved`` — только
       человек (``confirmed_by``) или названное правило владельца
       (``confirmed_rule`` + ``rule_version``), с датой и основанием; без
       них база откажет (``<класс>_approved_requires_provenance``).
       Система сама в ``approved`` не переводит.
    4. **Какое состояние пускает в клиентский путь знания.** Только
       ``approved`` при ``claim_scope = supported`` и не истёкшем
       ``valid_until`` — см. :func:`services.capabilities.client_facing_capabilities`.

    **У запрещённого есть предмет** (решение владельца 02.10, блок C;
    DRF-2726). Что именно нельзя утверждать, хранится у самого утверждения —
    в ``prohibited_statement``, а не прозой в ``limitations`` и не отдельным
    списком фраз. Поле существует для ВНУТРЕННЕГО проверяющего: чтобы он мог
    опознать нарушение и честно ответить на прямой вопрос. Клиенту оно не
    говорится и в контекст генерации клиентской рекомендации не входит —
    модель, получившая текст запрещённого обещания, способна его повторить.

    Заполняется только у ``claim_scope = prohibited_claim`` (ограничение базы,
    у любой строки), а у подтверждённого запрета обязательно (ограничение базы,
    у подтверждённой строки). Читателя этого поля здесь нет: кто и как отдаёт
    запрещённое проверяющему, решает мост чтения, не носитель.
    """

    class Status(models.TextChoices):
        #: Вывод системы или черновик — человеку не говорится.
        SYSTEM_INFERENCE = "system_inference", "System inference"
        #: Подтверждено человеком или названным правилом владельца.
        APPROVED = "approved", "Approved"

    class ClaimScope(models.TextChoices):
        SUPPORTED = "supported", "Supported"
        NOT_SUPPORTED = "not_supported", "Not supported"
        #: Обещание, которое произносить нельзя (§12 владельца).
        PROHIBITED_CLAIM = "prohibited_claim", "Prohibited claim"

    class EvidenceKind(models.TextChoices):
        """Вид доказательства — закрытый список (решение владельца №4 от 02.10).

        Восемь видов названы владельцем; порядок — его. «Маркетинг салона» сюда
        не входит: это не вид доказательства, а то, чем доказательство не
        является.
        """

        CLINICAL_GUIDELINE = "clinical_guideline", "Клиническая рекомендация"
        SYSTEMATIC_REVIEW = "systematic_review", "Систематический обзор / мета-анализ"
        RCT = "rct", "Рандомизированное контролируемое исследование"
        MANUFACTURER_IFU = "manufacturer_ifu", "Официальная инструкция производителя"
        REGULATORY_DOCUMENT = "regulatory_document", "Регуляторный документ"
        PROFESSIONAL_CONSENSUS = "professional_consensus", "Профессиональный консенсус"
        LEGAL_RULE = "legal_rule", "Правовая норма"
        PRODUCT_POLICY = "product_policy", "Продуктовая политика"

    class ClaimType(models.TextChoices):
        """Тип утверждения — по нему решается, нужен ли рецензент (DRF-2726).

        Список — УМОЛЧАНИЕ из слов владельца 02.10 (блок A: «проф./физиол./
        мед. утверждения»), а не его решение; меняется миграцией.
        """

        #: Тип не указан. С ним строку подтвердить нельзя.
        UNCLASSIFIED = "unclassified", "Не указан"
        #: Продуктовое / организационное — рецензент не нужен.
        PRODUCT = "product", "Продуктовое"
        PROFESSIONAL = "professional", "Профессиональное"
        PHYSIOLOGICAL = "physiological", "Физиологическое"
        MEDICAL = "medical", "Медицинское"

    #: Типы, которые подтверждаются только после проверки назначенным
    #: рецензентом. УМОЛЧАНИЕ: все, кроме ``product`` (главный выбор владельца).
    REVIEW_REQUIRED_CLAIM_TYPES = ("professional", "physiological", "medical")

    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.SYSTEM_INFERENCE,
    )
    claim_scope = models.CharField(
        max_length=24, choices=ClaimScope.choices, default=ClaimScope.NOT_SUPPORTED,
    )
    #: Что ограничивает утверждение (показания, условия, кому не подходит).
    limitations = models.TextField(blank=True, default="")
    #: Предмет запрета: что именно нельзя утверждать. Только у
    #: ``prohibited_claim``; для внутреннего проверяющего, не для клиента.
    prohibited_statement = models.TextField(blank=True, default="")
    #: Откуда знание — публикация, протокол, практика салона.
    evidence_source = models.CharField(max_length=300, blank=True, default="")
    #: Вид доказательства — из закрытого списка (решение владельца №4 от 02.10,
    #: DRF-2742). У черновика может быть пуст; у подтверждённой строки —
    #: обязателен (ограничение базы ниже). Маркетинг салона в списке нет
    #: намеренно: доказательством он не является.
    evidence_kind = models.CharField(
        max_length=64, blank=True, default="", choices=EvidenceKind.choices,
    )
    # DRF-2612: PROTECT, не SET_NULL — CHECK модели требует это поле непустым
    # у подтверждённой строки; обнуление при удалении User нарушило бы его.
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="+",
    )
    confirmed_rule = models.CharField(max_length=100, blank=True, default="")
    rule_version = models.CharField(max_length=32, blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    source_ref = models.CharField(max_length=200, blank=True, default="")
    #: Срок годности подтверждения; NULL — бессрочно. Истёкшее не говорится.
    valid_until = models.DateTimeField(null=True, blank=True)

    # DRF-2726 (блок A): «рецензент проверил факт» — отдельное решение от
    # «куратор утвердил использование» (``confirmed_by``), со своим автором.
    claim_type = models.CharField(
        max_length=16, choices=ClaimType.choices, default=ClaimType.UNCLASSIFIED,
    )
    # PROTECT по той же причине, что у ``confirmed_by`` (DRF-2612): CHECK ниже
    # требует это поле непустым у подтверждённой строки проверяемого типа.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        constraints = [
            # Подтверждение — решение, и без автора оно через месяц
            # неотличимо от умолчания. Форма та же, что у
            # `salonservice_verified_requires_provenance`.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | (
                        models.Q(confirmed_at__isnull=False)
                        & ~models.Q(source_ref="")
                        & (
                            models.Q(confirmed_by__isnull=False)
                            | ~models.Q(confirmed_rule="")
                        )
                    )
                ),
                name="%(class)s_approved_requires_provenance",
            ),
            # Предмет запрета — только у запрещённого утверждения: текст
            # запрещённого обещания не должен лежать у строки, которая может
            # стать клиентской.
            models.CheckConstraint(
                condition=(
                    models.Q(prohibited_statement="")
                    | models.Q(claim_scope="prohibited_claim")
                ),
                name="%(class)s_prohibition_only_on_prohibited_claim",
            ),
            # Подтверждённый запрет без предмета исполнить нечем.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | ~models.Q(claim_scope="prohibited_claim")
                    | ~models.Q(prohibited_statement="")
                ),
                name="%(class)s_approved_prohibition_has_statement",
            ),
            # Подтверждённое опирается на доказательство названного вида —
            # одного из закрытого списка; пустой вид в список не входит.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | models.Q(
                        evidence_kind__in=[
                            "clinical_guideline", "systematic_review", "rct", "manufacturer_ifu",
                            "regulatory_document", "professional_consensus", "legal_rule",
                            "product_policy",
                        ]
                    )
                ),
                name="%(class)s_approved_evidence_kind_known",
            ),
            # Подтверждённое утверждение имеет тип: без типа неизвестно, нужен
            # ли ему рецензент.
            models.CheckConstraint(
                condition=~models.Q(status="approved") | ~models.Q(claim_type="unclassified"),
                name="%(class)s_approved_has_claim_type",
            ),
            # Тип, требующий рецензента, подтверждается только с отметкой проверки.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | ~models.Q(claim_type__in=["professional", "physiological", "medical"])
                    | (models.Q(reviewed_by__isnull=False) & models.Q(reviewed_at__isnull=False))
                ),
                name="%(class)s_approved_review_when_required",
            ),
            # Отметка проверки цельная: человек и время — вместе или никак.
            models.CheckConstraint(
                condition=(
                    (models.Q(reviewed_by__isnull=True) & models.Q(reviewed_at__isnull=True))
                    | (models.Q(reviewed_by__isnull=False) & models.Q(reviewed_at__isnull=False))
                ),
                name="%(class)s_review_is_whole",
            ),
        ]

    def is_client_facing(self, *, now) -> bool:
        """Можно ли сказать это человеку: подтверждено, поддержано, не истекло."""
        return (
            self.status == self.Status.APPROVED
            and self.claim_scope == self.ClaimScope.SUPPORTED
            and (self.valid_until is None or self.valid_until > now)
        )


class ProcedureCapability(ClaimEvidence):
    """Что процедура канона умеет — одна возможность одной процедуры.

    ``key`` — **стабильный машинный идентификатор смысла, а не производная от
    текста** (приёмка владельца 29.09, §4): ``key ≠ slugify(text_client)``.
    Правка ``text_client`` / ``text_professional`` не меняет ``key``, и никакой
    код не выводит его из формулировки — ключ задаёт тот, кто заводит
    возможность (пример владельца: ``temporary_relaxation``). Одна возможность
    встречается у многих процедур; устойчивый ключ позволит потом свести их в
    общий словарь, не перечитывая текст. Уникален в паре (шаблон, ключ).

    **Срок результата — не самостоятельное число** (решение владельца 02.10,
    блок B; DRF-2726). ``result_timeframe`` у ПОДТВЕРЖДЁННОЙ строки не может
    состоять из одних цифр и знаков («3», «3–5», «~10 %») и не хранится без
    оговорки о разбросе
    (``variability_note``) и основания (источник + ссылка) — ограничения базы
    ниже. Черновик база не судит: черновик человеку не говорится, а строки,
    заведённые до этого правила, не должны ронять миграцию. Входы — форма и
    засев — строже базы и спрашивают то же у любой строки
    (:func:`services.knowledge_intake.timeframe_errors`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: PROTECT: знание о процедуре не должно исчезать вместе с правкой канона.
    template = models.ForeignKey(
        ServiceTemplate, on_delete=models.PROTECT, related_name="capabilities",
    )
    key = models.SlugField(max_length=64)
    #: Формулировка для человека — то, что может прозвучать клиенту.
    text_client = models.TextField(blank=True, default="")
    #: Профессиональная формулировка — для мастера и разбора.
    text_professional = models.TextField(blank=True, default="")
    expected_effect = models.TextField(blank=True, default="")
    #: Когда ждать результат — словами, не числом, и вместе с оговоркой ниже.
    result_timeframe = models.CharField(max_length=200, blank=True, default="")
    #: Оговорка о разбросе срока — текст, не числа («зависит от исходного
    #: состояния»). То же поле и тот же смысл, что у связи с целью.
    variability_note = models.TextField(blank=True, default="")

    class Meta(ClaimEvidence.Meta):
        constraints = [
            *ClaimEvidence.Meta.constraints,
            models.UniqueConstraint(
                fields=["template", "key"], name="procedurecapability_template_key_uniq",
            ),
            # Подтверждённый срок — только с оговоркой о разбросе и основанием.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | models.Q(result_timeframe="")
                    | (
                        ~models.Q(variability_note="")
                        & ~models.Q(evidence_source="")
                        & ~models.Q(source_ref="")
                    )
                ),
                name="procedurecapability_approved_timeframe_grounded",
            ),
            # И словами: значение без единой буквы («3», «3–5», «~10 %») —
            # число, а не срок. Строже, чем у курса: там ловится только одно
            # число, и «3-5» проходит (названо пределом в DRF-2726).
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | models.Q(result_timeframe="")
                    | ~models.Q(result_timeframe__regex=TIMEFRAME_WITHOUT_WORDS)
                ),
                name="procedurecapability_approved_timeframe_in_words",
            ),
        ]
        ordering = ["template", "key"]
        # Право подтверждать — отдельное от права изменять (DRF-2726, решение
        # владельца 02.10, блок A). Кому его дать, решает владелец; здесь —
        # только носитель. Исполняет его форма админки (services/admin.py).
        permissions = [
            ("approve_procedurecapability", "Может подтверждать возможности процедур"),
            # DRF-2726: тип утверждения решает, нужен ли рецензент, — значит,
            # объявить утверждение «продуктовым» и подтвердить его без
            # рецензента не может тот же, кого тип ограничивает. Это право
            # держателя продуктовых границ (третья роль блока A); одно на обе
            # таблицы знания.
            (
                "approve_claim_without_reviewer",
                "Может подтверждать утверждения без рецензента (продуктовые границы)",
            ),
        ]

    def __str__(self) -> str:
        # Имя процедуры, а не её UUID: эту строку читает куратор — в списке,
        # в выборе возможности у связи с целью, в журнале админки.
        return f"{self.template.name} · {self.key}"


class CapabilityGoalLink(ClaimEvidence):
    """Возможность процедуры → цель человека (``GoalOption``).

    Своя строка со своим основанием: «процедура умеет X» и «X помогает цели
    Y» — два утверждения, и подтверждаются они порознь.

    **Курс — здесь, на связи** (решение владельца 29.09): «обычно курс N» без
    названной цели — маркетинговое утверждение; осмысленно только «чтобы
    приблизиться к ЭТОЙ цели этой возможностью, обычно нужно…». Поэтому курс
    — часть claim и живёт под его основанием.

    **Голого числа хранить негде.** ``course_pattern`` не сохраняется без
    ``variability_note`` и основания (источник + ссылка) и не может быть
    одним числом — ограничения базы ниже. ``variability_note`` — ТЕКСТ с
    оговоркой, а не пара min/max: владелец запретил заставлять выдумывать
    точный диапазон там, где источник его не даёт.

    Предел, названный честно: это закрывает «в базе нет голого числа, которое
    можно процитировать», но НЕ закрывает персональное «тебе нужно 10».
    Обязательный будущий контракт читателя: **population-level knowledge ≠
    personal prescription** — подтверждённое общее утверждение само по себе
    не разрешает превращать его в предписание конкретному человеку.

    **Горизонт результата — под той же защитой** (решение владельца 02.10,
    блок B; DRF-2726): ``result_horizon`` у подтверждённой связи не может
    состоять из одних цифр и знаков и не хранится без ``variability_note`` и
    основания.
    Оговорка у связи одна — на курс и на горизонт. Черновик база не судит
    (причина — в докстринге :class:`ProcedureCapability`); входы спрашивают
    то же у любой строки.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        ProcedureCapability, on_delete=models.CASCADE, related_name="goal_links",
    )
    #: PROTECT: закрытый список целей решает владелец, связь его не правит.
    goal = models.ForeignKey(
        GoalOption, on_delete=models.PROTECT, related_name="capability_links",
    )
    #: Характер курса словами («обычно рассматривается как курс сеансов»).
    course_pattern = models.TextField(blank=True, default="")
    #: Когда ждать результат относительно этой цели — словами.
    result_horizon = models.CharField(max_length=200, blank=True, default="")
    #: Оговорка о разбросе — текст, не числа («зависит от исходного состояния»).
    variability_note = models.TextField(blank=True, default="")

    class Meta(ClaimEvidence.Meta):
        # Право подтверждать — отдельное от права изменять; см. ProcedureCapability.
        permissions = [
            ("approve_capabilitygoallink", "Может подтверждать связи возможностей с целями"),
        ]
        constraints = [
            *ClaimEvidence.Meta.constraints,
            models.UniqueConstraint(
                fields=["capability", "goal"], name="capabilitygoallink_capability_goal_uniq",
            ),
            # Курс — только вместе с оговоркой о разбросе и основанием.
            models.CheckConstraint(
                condition=(
                    models.Q(course_pattern="")
                    | (
                        ~models.Q(variability_note="")
                        & ~models.Q(evidence_source="")
                        & ~models.Q(source_ref="")
                    )
                ),
                name="capabilitygoallink_course_requires_variability_and_evidence",
            ),
            # И не одним числом: «10» в чистом виде хранить негде.
            models.CheckConstraint(
                condition=~models.Q(course_pattern__regex=r"^\s*[0-9]+([.,][0-9]+)?\s*$"),
                name="capabilitygoallink_course_not_a_bare_number",
            ),
            # Подтверждённый горизонт результата — как срок у возможности.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | models.Q(result_horizon="")
                    | (
                        ~models.Q(variability_note="")
                        & ~models.Q(evidence_source="")
                        & ~models.Q(source_ref="")
                    )
                ),
                name="capabilitygoallink_approved_horizon_grounded",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | models.Q(result_horizon="")
                    | ~models.Q(result_horizon__regex=TIMEFRAME_WITHOUT_WORDS)
                ),
                name="capabilitygoallink_approved_horizon_in_words",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.capability} → {self.goal.label}"


class ClaimApprovalReset(models.Model):
    """Журнал снятых подтверждений: что, когда и почему вернулось в черновик (DRF-2726).

    Требование владельца: снятие подтверждения фиксируется — причина, старое и
    новое значение, время, затронутые утверждения — и показывается куратору
    как необходимость повторной проверки. Строка — одно затронутое
    утверждение одного события; старое и новое значение лежат в ``changes``
    (внутренний аудит куратора: содержание здесь допустимо, наружу и в журнал
    приложения оно не уходит).

    Кто сделал правку, строка не хранит: это уже записано в журнале админки
    (``LogEntry``) у процедуры или утверждения.
    """

    class Reason(models.TextChoices):
        #: Правка типа, содержания или источника самого утверждения.
        CLAIM_EDITED = "claim_edited", "Изменено само утверждение"
        #: Правка возможности, о которой говорит связь с целью.
        CAPABILITY_EDITED = "capability_edited", "Изменена возможность, о которой связь"
        #: Правка значимого поля процедуры.
        PROCEDURE_CHANGED = "procedure_changed", "Изменены значимые данные процедуры"
        #: Категория процедуры перенесена под другой корень (другая область).
        CATEGORY_MOVED = "category_moved", "Категория процедуры перенесена"
        #: Изменена цель, о которой говорит связь.
        GOAL_CHANGED = "goal_changed", "Изменена цель, о которой связь"
        #: В области применения противопоказания появилась или исчезла процедура
        #: либо категория (DRF-2741).
        SCOPE_CHANGED = "scope_changed", "Изменился состав области применения"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=24, choices=Reason.choices)
    # SET_NULL, не CASCADE: журнал переживает удаление утверждения (иначе
    # удаление стирало бы след), а само удаление не упирается в журнал,
    # который в админке только читается. Что это было за утверждение, после
    # удаления говорят ``claim_kind`` и ``claim_label``.
    capability = models.ForeignKey(
        ProcedureCapability, on_delete=models.SET_NULL, null=True, blank=True, related_name="approval_resets",
    )
    goal_link = models.ForeignKey(
        CapabilityGoalLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="approval_resets",
    )
    contraindication = models.ForeignKey(
        "services.ProcedureContraindication", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approval_resets",
    )
    claim_kind = models.CharField(
        max_length=16,
        choices=[
            ("capability", "Возможность"), ("goal_link", "Связь с целью"),
            ("contraindication", "Противопоказание"),
        ],
    )
    claim_label = models.CharField(max_length=300, blank=True, default="")
    #: Когда утверждение подтвердили заново. Пусто — повторная проверка ещё нужна.
    resolved_at = models.DateTimeField(null=True, blank=True)
    #: Что изменилось: ``[{"field": …, "old": …, "new": …}]``.
    changes = models.JSONField(default=list)
    was_approved = models.BooleanField(default=False)
    had_review = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Строка — не более чем об одном утверждении (после его удаления — ни об одном).
            models.CheckConstraint(
                condition=(
                    (models.Q(capability__isnull=True) & models.Q(goal_link__isnull=True))
                    | (models.Q(capability__isnull=True) & models.Q(contraindication__isnull=True))
                    | (models.Q(goal_link__isnull=True) & models.Q(contraindication__isnull=True))
                ),
                name="claimapprovalreset_at_most_one_claim",
            ),
        ]

    @property
    def claim(self):
        return self.capability or self.goal_link or self.contraindication or self.claim_label

    @staticmethod
    def kind_of(claim) -> str:
        if isinstance(claim, ProcedureCapability):
            return "capability"
        if isinstance(claim, CapabilityGoalLink):
            return "goal_link"
        return "contraindication"

    @classmethod
    def resolve_for(cls, claim) -> int:
        """Утверждение подтверждено заново — его открытые записи закрываются."""
        return cls.objects.filter(
            resolved_at__isnull=True, **{cls.kind_of(claim): claim},
        ).update(resolved_at=timezone.now())

    def describe(self) -> str:
        """Одна строка для куратора: причина и что изменилось."""
        what = "; ".join(
            f"{c.get('field')}: «{c.get('old')}» → «{c.get('new')}»" for c in self.changes
        ) or "—"
        return f"{self.get_reason_display()}. {what}"

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} · {self.claim} · {self.get_reason_display()}"


class ClaimReviewer(models.Model):
    """Назначение рецензента: кто вправе проверять утверждения какого типа (DRF-2726).

    Решение владельца 02.10 (блок A): медицинские и физиологические
    утверждения проверяет «только назначенный рецензент подходящей
    квалификации»; «тело/массаж и кожа лица — возможно, разные люди». Строка
    этой таблицы и есть назначение. Имён в коде нет — строки заводит владелец
    в админке.

    ``category`` — область компетенции: корневая категория каталога, покрывает
    её и её подкатегории. Пусто — любая область.

    Право администратора компетенцией не является: суперпользователь без
    строки здесь проверить не может (:func:`services.knowledge_review.may_review`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: учётка сотрудника физически не удаляется (стирание — tombstone),
    # а назначение — след того, кто был вправе проверять.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="claim_reviewer_appointments",
    )
    claim_type = models.CharField(max_length=16, choices=ClaimEvidence.ClaimType.choices)
    category = models.ForeignKey(
        ServiceCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Назначать можно только на тип, который рецензент проверяет.
            models.CheckConstraint(
                condition=models.Q(claim_type__in=["professional", "physiological", "medical"]),
                name="claimreviewer_type_is_reviewable",
            ),
            models.UniqueConstraint(
                fields=["user", "claim_type", "category"],
                condition=models.Q(category__isnull=False),
                name="claimreviewer_user_type_category_uniq",
            ),
            models.UniqueConstraint(
                fields=["user", "claim_type"],
                condition=models.Q(category__isnull=True),
                name="claimreviewer_user_type_any_category_uniq",
            ),
        ]
        ordering = ["user", "claim_type"]

    def clean(self) -> None:
        if self.category_id is not None and self.category.parent_id is not None:
            raise ValidationError(
                {"category": "Область компетенции — корневая категория; она покрывает свои подкатегории."}
            )
        if self.claim_type and self.claim_type not in ClaimEvidence.REVIEW_REQUIRED_CLAIM_TYPES:
            raise ValidationError(
                {"claim_type": "Рецензент назначается только на тип утверждения, требующий проверки."}
            )

    def __str__(self) -> str:
        area = self.category.name if self.category_id else "любая область"
        return f"{self.user} · {self.get_claim_type_display()} · {area}"


class ProcedureContraindication(models.Model):
    """Противопоказание: условие → что делать (DRF-2741, слот C8).

    Решение владельца №2 от 02.10: C8 хранится структурированным набором —
    условие, действие из закрытого списка, источник, область применения, дата
    пересмотра, рецензент; «один непроверяемый текстовый блок недостаточен».
    Строка — одно условие и одно действие; общий набор для нескольких процедур
    — одна строка с несколькими процедурами в области применения, а не копии.

    Это НОСИТЕЛЬ. Читателя на пути ответа нет, клиенту отсюда ничего не
    говорится; текстовое поле ``ServiceTemplate.contraindications`` и гейт
    здоровья живут как жили. Набор — маршрутизация, а не диагностика:
    отсутствие условия в таблице не доказывает безопасность человеку.

    Две подписи, как у остального знания (DRF-2726): ``reviewed_by`` —
    рецензент проверил условие и действие, ``confirmed_by`` — куратор утвердил
    использование. C8 владелец назвал требующим профильной проверки ВСЕГДА,
    поэтому типа утверждения у строки нет: проверяет её назначенный рецензент
    медицинских утверждений (:data:`REVIEW_CLAIM_TYPE`), чьи назначения
    покрывают всю область применения строки.

    Подтверждённая строка без источника, вида доказательства, даты пересмотра,
    отметки рецензента и подписи не хранится (ограничение базы). Область
    применения — связи «многие ко многим», базой не проверяются: её у
    подтверждённой строки спрашивают входы.

    Правило сброса то же, что у остального знания: правка содержания или
    области снимает подтверждение; значимая правка процедуры из области
    применения возвращает строку в черновик
    (:func:`reset_claims_of_procedure`). Область меняется и без правки самой
    строки — когда процедура или категория из неё удалена, а в категорию
    пришла новая процедура; это тоже сброс (:func:`contraindications_in_scope`).

    Предел: прямые ``rule.templates.add()`` / ``.remove()`` из кода правило
    сброса не видят — как ``QuerySet.update()`` у остального знания; не видят
    его и процедуры, заведённые мимо ``save()`` (``bulk_create``,
    ``loaddata``). В дереве таких писателей нет: область пишут форма админки
    и засев новой строки, процедуры — ``save()``.

    При удалении категории вместе с её процедурами журнал называет удалённую
    процедуру, а не категорию: процедуры уходят первыми, и правилу после
    первой из них терять уже нечего.

    ``key`` — стабильный машинный идентификатор, как у возможности: по нему
    засев из файла узнаёт уже заведённую строку.
    """

    #: Тип утверждения, рецензент которого проверяет противопоказания.
    REVIEW_CLAIM_TYPE = "medical"

    class Action(models.TextChoices):
        """Что делать при условии — закрытый список (решение владельца №2)."""

        EXCLUDE = "exclude", "Исключить (не проводить / исключить зону)"
        POSTPONE = "postpone", "Отложить"
        REFER_TO_DOCTOR = "refer_to_doctor", "Направить к врачу"
        EMERGENCY = "emergency", "Экстренная помощь"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=64, unique=True)
    #: Условие — словами: при чём правило срабатывает.
    condition = models.TextField()
    action = models.CharField(max_length=16, choices=Action.choices)
    #: Уточнение действия («исключить зону», «только после разрешения врача»).
    action_note = models.TextField(blank=True, default="")
    #: Область применения: процедуры канона и/или категории (класс процедур).
    templates = models.ManyToManyField(ServiceTemplate, blank=True, related_name="contraindication_rules")
    categories = models.ManyToManyField(ServiceCategory, blank=True, related_name="+")
    #: Уточнение области словами («особенно при интенсивной технике»).
    scope_note = models.TextField(blank=True, default="")

    source_ref = models.CharField(max_length=200, blank=True, default="")
    evidence_source = models.CharField(max_length=300, blank=True, default="")
    evidence_kind = models.CharField(
        max_length=64, blank=True, default="", choices=ClaimEvidence.EvidenceKind.choices,
    )
    #: Когда строку нужно проверить заново.
    review_date = models.DateField(null=True, blank=True)

    # PROTECT у обеих подписей — по той же причине, что у ``confirmed_by``
    # знания (DRF-2612): CHECK ниже требует их у подтверждённой строки.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=24, choices=ClaimEvidence.Status.choices, default=ClaimEvidence.Status.SYSTEM_INFERENCE,
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        permissions = [
            ("approve_procedurecontraindication", "Может подтверждать противопоказания"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(condition=""), name="procedurecontraindication_condition_not_blank",
            ),
            models.CheckConstraint(
                condition=models.Q(action__in=["exclude", "postpone", "refer_to_doctor", "emergency"]),
                name="procedurecontraindication_action_known",
            ),
            # Отметка проверки цельная: человек и время — вместе или никак.
            models.CheckConstraint(
                condition=(
                    (models.Q(reviewed_by__isnull=True) & models.Q(reviewed_at__isnull=True))
                    | (models.Q(reviewed_by__isnull=False) & models.Q(reviewed_at__isnull=False))
                ),
                name="procedurecontraindication_review_is_whole",
            ),
            # Подтверждённое противопоказание — с источником, видом
            # доказательства, датой пересмотра и обеими подписями.
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="approved")
                    | (
                        ~models.Q(source_ref="")
                        & models.Q(
                            evidence_kind__in=[
                                "clinical_guideline", "systematic_review", "rct", "manufacturer_ifu",
                                "regulatory_document", "professional_consensus", "legal_rule",
                                "product_policy",
                            ]
                        )
                        & models.Q(review_date__isnull=False)
                        & models.Q(reviewed_by__isnull=False)
                        & models.Q(reviewed_at__isnull=False)
                        & models.Q(confirmed_by__isnull=False)
                        & models.Q(confirmed_at__isnull=False)
                    )
                ),
                name="procedurecontraindication_approved_complete",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} → {self.get_action_display()}"


# Удаление процедуры или категории убирает её из области применения молча:
# строки связи «многие ко многим» уходят каскадом, а правка строки при этом не
# происходит. Подтверждённое противопоказание осталось бы подтверждённым с
# другой — возможно, пустой — областью (DRF-2741). ``pre_delete``, а не
# ``delete()`` модели: при каскаде от категории ``delete()`` процедуры не
# вызывается. Сигнал идёт внутри транзакции удаления.


@receiver(pre_delete, sender=ServiceTemplate)
def _a_deleted_procedure_leaves_the_scope(sender: Any, instance: Any, **kwargs: Any) -> None:
    reset_contraindications(
        contraindications_in_scope(template=instance, category_ids=[instance.category_id]),
        reason=ClaimApprovalReset.Reason.SCOPE_CHANGED,
        changes=[{"field": "templates", "old": instance.name, "new": ""}],
    )


@receiver(pre_delete, sender=ServiceCategory)
def _a_deleted_category_leaves_the_scope(sender: Any, instance: Any, **kwargs: Any) -> None:
    reset_contraindications(
        contraindications_in_scope(category_ids=[instance.pk]),
        reason=ClaimApprovalReset.Reason.SCOPE_CHANGED,
        changes=[{"field": "categories", "old": instance.name, "new": ""}],
    )
