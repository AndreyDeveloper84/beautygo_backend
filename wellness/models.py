"""Каркас измеримых целей (домен wellness) — первая миграция.

Контракт: docs/PROPOSAL_GOALS_MODEL_FINAL.md §1–§8 (GOALS-R1..R6,
amendments A–E). Разрешена только схема + гейты fail-closed:
persistent writes НЕ включаются до Gate D (scope `goal_memory`)
и Gate O (Registry amendment + Privacy/Legal + verified consent
integration) — см. docs/OD_GOALS_RULINGS.md.

Ключ владения — человек (`settings.AUTH_USER_MODEL`), tenant-less:
прецедент `goals.ClientGoal` (SPEC §6.2). `ACHIEVED`/`FAILED`
отсутствуют во всех перечислениях: система физически не может
объявить цель достигнутой или проваленной.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class DesiredOutcome(models.Model):
    """Желаемый результат человека — 0..N активных с первого дня (§2).

    Аналога `clientgoal_one_active_per_client` НЕТ — это осознанное
    требование (§2): активных результатов может быть несколько.

    Инвариант OD-DC-1: заполнено хотя бы одно из `direction` /
    `desired_state_numeric` (CheckConstraint ниже).
    """

    class Direction(models.TextChoices):
        REDUCE = "reduce", "Снизить"
        INCREASE = "increase", "Увеличить"
        MAINTAIN = "maintain", "Поддерживать"

    class Status(models.TextChoices):
        OPEN = "open", "Открыт"
        CLOSED_BY_USER = "closed_by_user", "Закрыт пользователем"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="desired_outcomes",
    )
    target = models.SlugField(
        max_length=64,
        help_text="Ключ объекта результата (напр. body_weight, edema)",
    )
    statement_text = models.TextField(
        help_text="Дословная формулировка пользователя; не нормализуется",
    )
    direction = models.CharField(
        max_length=16,
        choices=Direction.choices,
        null=True,
        blank=True,
        help_text="Направление изменения; NULL при заданном desired_state_numeric",
    )
    desired_state_numeric = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Желаемое числовое состояние; NULL при заданном direction",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(direction__isnull=False)
                    | models.Q(desired_state_numeric__isnull=False)
                ),
                name="desiredoutcome_direction_or_numeric_present",
            ),
        ]

    def __str__(self) -> str:
        return f"DesiredOutcome<{self.user_id}> {self.target} (status={self.status})"


class PersonalPlan(models.Model):
    """Персональный план — общий контейнер, 0..1 ACTIVE на человека (OD-GOAL-4).

    Закрытые ряды (`closed_by_user`) — история; отдельного журнала нет.
    Сменить план может только человек (§6).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        CLOSED_BY_USER = "closed_by_user", "Закрыт пользователем"
        # DRF-2857 — закрыт тем, что человек сохранил durable-план
        # (``wellness.Plan``). Это НЕ ``closed_by_user``: человек не говорил,
        # что этот план вести не хочет, — он подтвердил новый. Разведено по
        # образцу ``ClientGoal.State.SUPERSEDED``; писатель один —
        # ``wellness.plan_engine.create_plan_from_command``.
        SUPERSEDED = "superseded", "Замещён сохранённым планом"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="personal_plans",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    # DRF-2101 (Plan Lite, §49) — план держит цель САМ: ссылка на уже
    # данную ``goals.ClientGoal`` (та живёт под своим основанием) и её ключ
    # снимком для чтения без join. Не DesiredOutcome: тот пишется только
    # через ``record_outcome`` под гейтом D (GOALS-R6), а §49 санкционировал
    # план из действий, не хранение результата. SET_NULL, не CASCADE:
    # ``PlanAction.plan`` — PROTECT, и каскад из цели в план упал бы на нём;
    # план переживает снятие цели со своим ``goal_key``.
    goal = models.ForeignKey(
        "goals.ClientGoal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plans",
    )
    goal_key = models.SlugField(
        max_length=64,
        blank=True,
        default="",
        help_text="Снимок ClientGoal.goal_key на момент составления плана (Plan Lite)",
    )
    # DRF-2123 (§51) — откуда план: ``manual`` или ``template:<goal_key>:v<N>``
    # при составлении по предложенному шаблону. Строка, не FK: шаблон —
    # курируемые данные, они версионируются и не удаляются; ПДн нет.
    source = models.CharField(
        max_length=96,
        default="manual",
        help_text="manual | template:<goal_key>:v<N> — по какому шаблону составлен (Plan Lite)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(status="active"),
                name="personalplan_one_active_per_user",
            ),
        ]

    def __str__(self) -> str:
        return f"PersonalPlan<{self.user_id}> (status={self.status})"


class PlanOutcomeLink(models.Model):
    """Связь плана и результата — ≤1 ACTIVE на outcome (GOALS-R4).

    Продолжение результата в новом плане — новая строка связи; старая
    `closed_by_user` остаётся историей (§3, amendment A). Результат не
    клонируется; новый план baseline не сбрасывает — baseline привязан
    к outcome, не к связи.

    `target_date` NULL легален — цель без срока.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Активна"
        CLOSED_BY_USER = "closed_by_user", "Закрыта пользователем"

    class HorizonStatus(models.TextChoices):
        NONE = "none", "Без срока"
        UPCOMING = "upcoming", "Срок не наступил"
        ELAPSED = "elapsed", "Срок прошёл"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        PersonalPlan,
        on_delete=models.PROTECT,
        related_name="outcome_links",
    )
    outcome = models.ForeignKey(
        DesiredOutcome,
        on_delete=models.PROTECT,
        related_name="plan_links",
    )
    target_date = models.DateField(
        null=True,
        blank=True,
        help_text="Срок цели; NULL = цель без срока",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["outcome"],
                condition=models.Q(status="active"),
                name="planoutcomelink_one_active_per_outcome",
            ),
        ]

    @property
    def horizon_status(self) -> str:
        """Вычислимое состояние горизонта (§6), НЕ колонка.

        target_date IS NULL -> NONE; today <= target_date -> UPCOMING;
        иначе ELAPSED. ELAPSED — context fact: автозакрытия нет,
        Progress не обнуляется, observations не удаляются.
        """
        if self.target_date is None:
            return self.HorizonStatus.NONE
        if timezone.localdate() <= self.target_date:
            return self.HorizonStatus.UPCOMING
        return self.HorizonStatus.ELAPSED

    def __str__(self) -> str:
        return (
            f"PlanOutcomeLink<plan={self.plan_id} outcome={self.outcome_id}> "
            f"(status={self.status}, horizon={self.horizon_status})"
        )


class ProgressObservation(models.Model):
    """Наблюдение прогресса — типизированные колонки, не JSONB (§4).

    Первый срез (GOALS-R1), оба типа только `origin=user_stated`:
    - WEIGHT: `value_numeric` (kg, фиксирована), `instrument` NULL;
    - SELF_ASSESSMENT: `value_ordinal` 0–3 + обязательный
      `instrument=NOTICEABILITY_0_3_V1` (versioned scale code,
      amendment B), `value_numeric` NULL.

    `measured`/`inferred`/`derived` в перечислении origin НЕТ —
    выводимое наблюдение невозможно записать (структурный запрет).

    Исправление — append-only supersede (§8): старая строка получает
    `superseded_by` и исключается из прогресса, но хранится.
    """

    class ObservationType(models.TextChoices):
        WEIGHT = "weight", "Вес"
        SELF_ASSESSMENT = "self_assessment", "Самооценка"

    class Origin(models.TextChoices):
        USER_STATED = "user_stated", "Указано пользователем"

    #: Версионированный код шкалы заметности (amendment B). Смена
    #: формулировок — новая версия инструмента, не правка на месте.
    INSTRUMENT_NOTICEABILITY_0_3_V1 = "NOTICEABILITY_0_3_V1"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="progress_observations",
    )
    observation_type = models.CharField(
        max_length=32,
        choices=ObservationType.choices,
    )
    origin = models.CharField(
        max_length=16,
        choices=Origin.choices,
        default=Origin.USER_STATED,
    )
    instrument = models.SlugField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Версионированный код шкалы; обязателен для self_assessment, NULL для weight",
    )
    value_numeric = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Числовое значение (weight, unit=kg фиксирована)",
    )
    value_ordinal = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Порядковое значение 0–3 (self_assessment)",
    )
    observed_at = models.DateTimeField(default=timezone.now)
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="supersedes",
        help_text="Append-only исправление (§8): указывает на заменившую строку",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-observed_at"]
        constraints = [
            # Заполнена ровно колонка своего типа (§4): либо валидный
            # weight, либо валидный self_assessment — ничего между.
            models.CheckConstraint(
                check=(
                    models.Q(
                        observation_type="weight",
                        value_numeric__isnull=False,
                        value_ordinal__isnull=True,
                        instrument__isnull=True,
                    )
                    | models.Q(
                        observation_type="self_assessment",
                        value_ordinal__isnull=False,
                        instrument__isnull=False,
                        value_numeric__isnull=True,
                    )
                ),
                name="progressobservation_value_matches_type",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "observation_type", "observed_at"],
                name="progressobs_user_type_time_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ProgressObservation<{self.user_id}> {self.observation_type}"
            f" @ {self.observed_at:%Y-%m-%d}"
        )


class EvidenceRegistryEntry(models.Model):
    """Evidence Registry — курируемая допустимость наблюдений (§5).

    Ключ — четвёрка (amendment B): (outcome_target, observation_type,
    origin, instrument) + approved_by/approved_at. Изменение записи —
    отдельный owner approval (GOALS-R1). Запись наблюдения валидируется
    против реестра fail-closed (сейчас — всегда отказ, см. services.py).

    `instrument` — CharField с default="" (не NULL): иначе unique_together
    перестаёт работать, т.к. NULL не равен NULL.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    outcome_target = models.SlugField(
        max_length=64,
        help_text="Ключ объекта результата (DesiredOutcome.target)",
    )
    observation_type = models.CharField(
        max_length=32,
        choices=ProgressObservation.ObservationType.choices,
    )
    origin = models.CharField(
        max_length=16,
        choices=ProgressObservation.Origin.choices,
    )
    instrument = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Код шкалы; пустая строка для типов без инструмента (weight)",
    )
    approved_by = models.CharField(max_length=255)
    approved_at = models.DateTimeField()

    class Meta:
        ordering = ["outcome_target"]
        unique_together = [
            ("outcome_target", "observation_type", "origin", "instrument"),
        ]

    def __str__(self) -> str:
        return (
            f"EvidenceRegistryEntry({self.outcome_target}, {self.observation_type},"
            f" {self.origin}, {self.instrument or '—'})"
        )


class PlanAction(models.Model):
    """Плановое обязательство внутри Personal Plan (DRF-1334, контракт §2).

    Запись «что обещано» — НЕ engine: ни расписания, ни напоминаний, ни
    пересчёта. Сопоставление с фактами — отдельная производная
    (`wellness/adherence.py`).

    `action_type` — курируемый ключ: первый срез — `log_food`, `log_water`
    (ровно под факты, которые Nutrition уже сообщает); новый ключ — owner
    approval, как в Evidence Registry.

    Каденс сознательно бедный (вердикт 25.08): конкретные дни и время суток
    — уже суждение о том, *когда* человек что-то сделал, а это за рубежом
    «утверждение о плане, не о человеке».
    """

    class ActionType(models.TextChoices):
        LOG_FOOD = "log_food", "Запись питания"
        LOG_WATER = "log_water", "Запись воды"
        # DRF-2101 — расширение по решению владельца §49 (Plan Lite):
        # «записаться на услугу под цель». Факт — бронь, не визит и не
        # результат визита.
        BOOK_SERVICE = "book_service", "Запись на услугу"

    class Cadence(models.TextChoices):
        PER_DAY = "per_day", "Раз в день"
        PER_WEEK = "per_week", "Раз в неделю"
        # DRF-2123 (§51): ведро — 14 дней ОТ СОЗДАНИЯ ПЛАНА (текущее
        # содержит «сейчас»), не календарная неделя.
        PER_2_WEEKS = "per_2_weeks", "Раз в две недели"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        PersonalPlan,
        on_delete=models.PROTECT,
        related_name="actions",
    )
    action_type = models.CharField(
        max_length=32,
        choices=ActionType.choices,
        help_text="Курируемый ключ обязательства; расширение — owner approval",
    )
    cadence = models.CharField(
        max_length=16,
        choices=Cadence.choices,
    )
    target_count = models.PositiveSmallIntegerField(
        default=1,
        help_text="Сколько раз за ведро каденса (день для per_day, неделя для per_week, 14 дней для per_2_weeks)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["plan"], name="planaction_plan_idx"),
        ]

    def __str__(self) -> str:
        return (
            f"PlanAction<plan={self.plan_id}> {self.action_type}"
            f" {self.target_count}x {self.cadence}"
        )


#: Значения ``NutritionProfile.goal`` анкеты питания — язык, на котором говорит
#: подсказка (DRF-2124, В-4). Строки, не импорт из ``nutrition``: wellness не
#: тянет профиль питания (В-1), а словарь анкеты закрыт решением владельца §85.
NUTRITION_GOAL_HINT_VALUES: tuple[str, ...] = ("lose", "maintain", "gain")


def validate_nutrition_goal_hint(value: object) -> None:
    """Подсказка — список значений анкеты, без повторов. Сид с опечаткой падает
    здесь, а не доезжает до человека как «обычно выбирают slim»."""
    from django.core.exceptions import ValidationError

    if not isinstance(value, list):
        raise ValidationError("nutrition_goal_hint must be a list")
    unknown = [v for v in value if v not in NUTRITION_GOAL_HINT_VALUES]
    if unknown:
        raise ValidationError(
            f"nutrition_goal_hint: unknown values {unknown!r}; allowed {list(NUTRITION_GOAL_HINT_VALUES)}"
        )
    if len(set(value)) != len(value):
        raise ValidationError("nutrition_goal_hint: duplicate values")


class PlanTemplate(models.Model):
    """Шаблон Plan Lite по цели — таблица владельца §51 как ДАННЫЕ (DRF-2123).

    Курируемая строка «цель → 1–3 обязательства + почему»: правится в
    админке, кладётся сидом ``seed_plan_templates`` (идемпотентно; изменение
    текста — новая ``version``, старая ``is_active=False``). Ровно одна
    активная на ``goal_key`` (частичная уникальность), версии не удаляются:
    ``PersonalPlan.source`` ссылается на них строкой.

    Не персональные данные: ни указателя на человека, ни строкового
    субъекта — в реестр стирания (``users/deletion_executor.py``) не входит.

    Категория для ``book_service`` здесь НЕ хранится — выводится из цели на
    стороне подбора (``goals.wiring.goal_category_ids_for_key``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    goal_key = models.SlugField(
        max_length=64,
        help_text="Ключ курируемой цели (services.GoalOption.key)",
    )
    actions = models.JSONField(
        default=list,
        help_text="[{action_type, cadence, target_count}] — 1–3 обязательства в форме PlanAction",
    )
    why_text = models.TextField(
        help_text="Слово владельца «почему такой план» — показывается человеку дословно",
    )
    # DRF-2124 (План-B, В-4) — ПОДСКАЗКА анкете питания: какой ``goal``
    # (lose/maintain/gain) обычно выбирают под эту курируемую цель. Список,
    # потому что у body_shape их две («lose или maintain» — выбор человека);
    # пустой — подсказки нет. Связь трёх понятий цели, не слияние: сюда не
    # пишется ничего из профиля, отсюда ничего не предвыбирается.
    nutrition_goal_hint = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_nutrition_goal_hint],
        help_text="Подсказка анкете питания: [] | [lose|maintain|gain, …] — что обычно выбирают под цель",
    )
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["goal_key", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["goal_key"],
                condition=models.Q(is_active=True),
                name="plantemplate_one_active_per_goal_key",
            ),
            models.UniqueConstraint(
                fields=["goal_key", "version"],
                name="plantemplate_goal_key_version_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"PlanTemplate<{self.goal_key} v{self.version}{'' if self.is_active else ' inactive'}>"


# ─── Plan Engine: durable Plan / PlanRevision (DRF-2857, WP1) ────────────────
#
# Контракт: PLAN_ENGINE_CONTRACT v1.0 §4.4–§4.9 (тело без изменений в v1.1).
# Хранение — здесь; композиция и семантическая валидация — ayla-ai-core
# (§10.1); Lite (``PersonalPlan``/``PlanAction``) живёт рядом под своим флагом
# и сюда не переезжает.

#: Закрытый список запрещённых полей контракта §4.8. Сверяется с текстом
#: контракта узлом (``wellness/tests/test_plan_engine_2857.py``), чтобы список
#: в коде не мог разойтись с контрактом молча.
PLAN_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "sub_steps", "children", "parts", "parent_step_id", "progress", "percent",
        "completed_count", "done", "score", "confidence", "next_visit_date", "course",
        "sessions_total", "sessions_done", "compatible_with", "incompatible_with",
        "repeat_every", "recovery_days",
    }
)

#: Шесть версий политик на ревизии (§4.1, §7) — ровно этот набор ключей.
PLAN_POLICY_VERSION_KEYS: frozenset[str] = frozenset(
    {
        "plan_spec_version", "constraint_policy_version", "resolver_spec_version",
        "catalog_mapping_version", "safety_policy_version", "reason_code_registry_version",
    }
)


class ImmutablePlanRecordError(RuntimeError):
    """Попытка изменить или удалить ревизию плана (§4.4: никогда не правится)."""


class _PlanRevisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutablePlanRecordError(
            "PlanRevision: update() запрещён — ревизия иммутабельна (контракт §4.4); "
            "изменение плана = новая ревизия"
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise ImmutablePlanRecordError("PlanRevision: bulk_update() запрещён — ревизия иммутабельна")

    def delete(self):
        raise ImmutablePlanRecordError(
            "PlanRevision: delete() запрещён — ревизии уходят только вместе с планом "
            "при стирании данных человека (каскад от Plan)"
        )


class Plan(models.Model):
    """Сохранённый план — durable, авторитетный (контракт §4.4).

    Создаётся только командой сохранения по явному подтверждению человека
    (§4.9, ``wellness.plan_engine.create_plan_from_command``); содержимое
    живёт в ревизиях. Не более одного ``active`` на цель (§4.7).

    ``goal`` nullable ТОЛЬКО ради ``SET_NULL``: план переживает снятие цели
    как история. Создать план без цели писатель не даёт — это открытый вопрос
    владельца Q-PE-2 (§16.2), и схема его не решает.

    Ни одного поля из закрытого списка §4.8 (``PLAN_FORBIDDEN_FIELDS``): ни
    прогресса, ни счётчиков, ни курса.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Действует"
        PAUSED = "paused", "Приостановлен"
        SUPERSEDED = "superseded", "Замещён другим планом той же цели"
        ARCHIVED = "archived", "В архиве"

    class CreatedVia(models.TextChoices):
        CONFIRMATION = "confirmation", "Подтверждение человека"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="engine_plans",
    )
    # ``related_name`` не ``plans``: то имя занято ``PersonalPlan.goal`` (Lite).
    goal = models.ForeignKey(
        "goals.ClientGoal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engine_plans",
        help_text="Цель плана; NULL — только после снятия цели (история), не при создании",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_via = models.CharField(
        max_length=16,
        choices=CreatedVia.choices,
        default=CreatedVia.CONFIRMATION,
    )
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        help_text="sha256(subject, decision_id, подтверждение) — §4.9; считает сервер",
    )
    # RESTRICT, не PROTECT: при стирании человека план удаляется вместе со
    # своими ревизиями (каскад ниже), и указатель на удаляемую ревизию этому
    # не мешает; удалить ревизию отдельно от плана по-прежнему нельзя.
    current_revision = models.ForeignKey(
        "wellness.PlanRevision",
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Текущая ревизия; NULL только внутри транзакции создания",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status_changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["goal"],
                condition=models.Q(status="active"),
                name="plan_one_active_per_goal",
            ),
        ]
        indexes = [
            models.Index(fields=["subject_user", "status"], name="plan_subject_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Plan<{self.subject_user_id}> goal={self.goal_id} (status={self.status})"


class PlanRevision(models.Model):
    """Ревизия плана — ИММУТАБЕЛЬНА (контракт §4.4, §9.1).

    Снимки, а не ссылки: шаги и утверждения хранятся целиком, чтобы ревизию
    можно было воспроизвести без текущего состояния каталога. Изменение плана
    — новая ревизия, эта строка не правится никогда. Удаляется только каскадом
    от ``Plan`` при стирании данных человека.

    ПДн-дисциплина §4.4: ссылки (``decision_id``, ``capability_ref``,
    ``assertion_id``) вместо текстов; свободного текста у шага нет (PE-2) —
    форму держит писатель.
    """

    class Staleness(models.TextChoices):
        NONE = "none", "Актуальна"
        STALE_RULES = "stale_rules", "Правила обновились"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_no = models.PositiveIntegerField(help_text="Монотонный в рамках плана, с 1")
    steps_snapshot = models.JSONField(help_text="PlanStep целиком (§4.2) — снимок, не ссылки")
    assertions_snapshot = models.JSONField(
        default=list, help_text="PlanningAssertion целиком — воспроизводимость (§4.4)",
    )
    validation = models.JSONField(help_text="PlanValidation на момент сборки (§6)")
    staleness = models.CharField(
        max_length=16,
        choices=Staleness.choices,
        default=Staleness.NONE,
    )
    policy_versions = models.JSONField(help_text="Шесть версий политик (§4.1)")
    created_from = models.JSONField(help_text="{decision_id} | {recompute_event}")
    content_hash = models.CharField(
        max_length=64,
        help_text="sha256 канонического JSON снимков — сверка повтора команды",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = _PlanRevisionQuerySet.as_manager()

    class Meta:
        ordering = ["plan", "revision_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "revision_no"],
                name="planrevision_plan_revision_no_unique",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutablePlanRecordError(
                f"PlanRevision {self.pk}: ревизия иммутабельна — изменение плана = новая ревизия"
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutablePlanRecordError(
            f"PlanRevision {self.pk}: delete() запрещён — ревизия уходит только вместе с планом"
        )

    def __str__(self) -> str:
        return f"PlanRevision<plan={self.plan_id}> #{self.revision_no}"
