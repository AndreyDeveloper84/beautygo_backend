"""Клиентская цель (Transformation Goal) — durable-факт выбора.

DRF-1190 / OD-1 (2026-08-19): цель — гибрид: курируемый ключ (`goal_key`)
из списка подсказок ИЛИ свободный текст (`goal_text`), сохраняемый
дословно — он является будущим корпусом формулировок (OD-2: корпуса нет,
пилот — механизм его сбора).

`ClientGoal` — факт («клиент выбрал/написал цель в момент времени»),
а НЕ проекция понимания. Эфемерный документ состояния (DecisionContext)
строится поверх этой таблицы и в БД не хранится — см. Ответ 3 главного
окна в docs/REPLY_CONVERSATION_ARCH.md: не смешивать их в одну таблицу.

Инварианты:
- хотя бы одно из `goal_key` / `goal_text` заполнено (CheckConstraint);
- одна активная цель на клиента (partial UniqueConstraint): смена цели
  закрывает прежний ряд (`is_active=False`) и создаёт новый — закрытые
  ряды и есть история, отдельного журнала не заводим.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ClientGoal(models.Model):
    """Выбранная клиентом цель — единственная активная на клиента."""

    class SourceChannel(models.TextChoices):
        BOT = "bot", "Бот (DM)"
        MINIAPP = "miniapp", "Mini App"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="client_goals",
    )
    # Салон, в котором цель названа (DRF-1455). Часть самого факта, а не
    # справка: распознавание названной услуги читает каталог, и читать
    # его надо каталогом ЭТОГО салона. Без поля решение принималось по
    # строкам всех салонов сразу — клиент салона A получал «цель
    # распознана» от услуги, которая есть только у салона B.
    #
    # null=True: строки, созданные до DRF-1455, и клиенты, у которых
    # салон не определяется однозначно (мультипровайдерный клиент с
    # несколькими активными связями — штатный случай #246). NULL здесь
    # значит «салон неизвестен», и тогда решение принимается только по
    # бессалонной части каталога — см. goals/service_match.py.
    #
    # PROTECT — как у остальных tenant-FK в репозитории: удаление салона
    # не должно молча уносить факты о целях его клиентов.
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="client_goals",
        help_text="Салон, в контексте которого цель названа; NULL — салон неизвестен",
    )
    # Ключ курируемой подсказки (services.GoalOption.key). NULL — цель
    # сформулирована свободным текстом и пока не отображена на ключ
    # (низкая уверенность -> уточнение, а не насильный маппинг; OD-1).
    goal_key = models.SlugField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Ключ курируемой цели (GoalOption.key); NULL при свободном вводе",
    )
    # Дословная формулировка пользователя. Хранится даже при распознанном
    # ключе — это будущий датасет формулировок (OD-2).
    goal_text = models.TextField(
        null=True,
        blank=True,
        help_text="Дословный свободный ввод пользователя; не нормализуется",
    )
    selected_at = models.DateTimeField(default=timezone.now)
    source_channel = models.CharField(
        max_length=16,
        choices=SourceChannel.choices,
        help_text="Канал, в котором цель выбрана (бот ↔ Mini App; цель переживает смену канала)",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-selected_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(goal_key__isnull=False)
                    | models.Q(goal_text__isnull=False)
                ),
                name="clientgoal_key_or_text_present",
            ),
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(is_active=True),
                name="clientgoal_one_active_per_client",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "is_active"],
                name="clientgoal_client_active_idx",
            ),
            # Салонные выборки («какие цели ставят клиенты салона X»)
            # и проверки границы тенанта. Тот же порядок колонок, что у
            # SalonService/User: tenant первым — по нему фильтруют всегда.
            models.Index(
                fields=["tenant", "is_active"],
                name="clientgoal_tenant_active_idx",
            ),
        ]

    def __str__(self) -> str:
        shown = self.goal_key or (self.goal_text or "")[:40]
        return f"ClientGoal<{self.client_id}> {shown} (active={self.is_active})"


class GoalAnketaRun(models.Model):
    """Один проход анкеты цели (DRF-1451).

    Почему проход — отдельная строка, а не поле у клиента: владелец
    распорядился (DRF-1225, подтверждено условием C-4 поправки A-1), что
    анкету можно проходить **сколько угодно раз**. Проход — это факт
    («человек начал отвечать тогда-то и закончил вот такой целью»), и
    таких фактов у клиента много. Незакрытый проход ровно один — его и
    ищет ``build_decision_context``.

    ``goal`` — цель, которой проход завершился. NULL у брошенного:
    человек в середине анкеты назвал услугу свободным вводом и ушёл к
    подбору. Это не ошибка, а разрешённый выход (C-2, «анкета не
    ворота»), поэтому проход закрывается без цели, а не висит открытым и
    не затягивает человека обратно в вопросы.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="goal_anketa_runs",
    )
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    goal = models.ForeignKey(
        "goals.ClientGoal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="anketa_runs",
        help_text="Цель, которой завершился проход; NULL — проход брошен",
    )

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client"],
                condition=models.Q(completed_at__isnull=True),
                name="goalanketarun_one_open_per_client",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "completed_at"],
                name="goalanketarun_client_open_idx",
            ),
        ]

    def __str__(self) -> str:
        state = "open" if self.completed_at is None else "closed"
        return f"GoalAnketaRun<{self.client_id}> {state}"


class GoalAnketaAnswer(models.Model):
    """Ответ на один шаг анкеты — durable-факт.

    Хранится дословно и не нормализуется по той же причине, что и
    ``ClientGoal.goal_text``: это будущий корпус формулировок (OD-2).
    Проекция (какой вопрос задавать следующим) строится поверх этих
    строк на каждый запрос и в БД не лежит.

    Собирается **только цель** — условие C-3 поправки A-1: ни контактов,
    ни телефона (DRF-1039), ни профильных полей здесь нет и быть не
    может, потому что список шагов закрыт в ``goals/anketa.py``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        GoalAnketaRun,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    step_key = models.SlugField(max_length=32)
    option_key = models.SlugField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Ключ выбранного варианта; NULL при свободном вводе",
    )
    answer_text = models.TextField(
        null=True,
        blank=True,
        help_text="Дословный свободный ввод; не нормализуется",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(option_key__isnull=False)
                    | models.Q(answer_text__isnull=False)
                ),
                name="goalanketaanswer_option_or_text_present",
            ),
            models.UniqueConstraint(
                fields=["run", "step_key"],
                name="goalanketaanswer_one_per_step",
            ),
        ]

    def __str__(self) -> str:
        shown = self.option_key or (self.answer_text or "")[:40]
        return f"GoalAnketaAnswer<{self.run_id}> {self.step_key}={shown}"
