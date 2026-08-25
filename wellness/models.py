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

    class Cadence(models.TextChoices):
        PER_DAY = "per_day", "Раз в день"
        PER_WEEK = "per_week", "Раз в неделю"

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
        help_text="Сколько раз за ведро каденса (день для per_day, неделя для per_week)",
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
