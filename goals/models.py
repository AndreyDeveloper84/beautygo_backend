"""Клиентская цель (Transformation Goal) — durable-факт выбора.

DRF-1190 / OD-1 (2026-08-19): цель — гибрид: курируемый ключ (`goal_key`)
из списка подсказок ИЛИ свободный текст (`goal_text`), сохраняемый
дословно — он является будущим корпусом формулировок (OD-2: корпуса нет,
пилот — механизм его сбора).

`ClientGoal` — факт («клиент выбрал/написал цель в момент времени»),
а НЕ проекция понимания. Эфемерный документ состояния (DecisionContext)
строится поверх этой таблицы и в БД не хранится — см. Ответ 3 главного
окна в docs/REPLY_CONVERSATION_ARCH.md: не смешивать их в одну таблицу.

Салона на цели НЕТ — и это решение, а не пропуск (DRF-1472, владелец
04.09.2026). DRF-1455 завёл `ClientGoal.tenant`, чтобы судить о цели по
каталогу одного салона; выяснилось, что цели спрашивает только
клиентский бот, а он один, общий, и салон у него не задан нарочно — это
витрина. Салонный бот обслуживает владельцев салонов и целей не
спрашивает. Значит поле не отвечало ни на один реальный вопрос, и
колонка снята (миграция `0005`).

Жизненный цикл (DRF-1660; решение владельца §97 OD-GOAL-B, 10.09.2026):

    ACTIVE     цель актуальна — Ayla ведёт по ней
    PAUSED     цель остаётся моей, но сейчас Ayla по ней не ведёт
    ACHIEVED   человек явно считает результат достигнутым
    ARCHIVED   человек больше не хочет вести эту цель

Прежний `is_active` bool это выразить не мог: единственный писатель
`False` сидел внутри «записать новую, закрыв прежнюю», то есть снять
цель без замещающей было нельзя — состояние, из которого нет выхода
(основание запрета — §122). Человек обязан уметь приостановить,
завершить и архивировать цель **без создания замещающей**; входы —
`goals/lifecycle.py` и `POST /internal/me/goals/state/`.

Два состояния сверх четырёх владельческих, оба — не догадка, а
названный факт:

- `SUPERSEDED` — закрыта тем, что человек выбрал новую цель, пока
  схема держит «одна ACTIVE на клиента». Это НЕ `ARCHIVED`: человек не
  говорил, что старую цель вести не хочет, — он назвал новую. Разводить
  их обязательно: `ARCHIVED` — слово человека, `SUPERSEDED` — следствие
  ограничения схемы; когда ограничение снимут (§97 OD-GOAL-E: несколько
  ACTIVE допустимы), `SUPERSEDED` перестанет писаться, а старые строки
  останутся читаемыми как то, чем были;
- `LEGACY_INACTIVE_REASON_UNKNOWN` — 33 строки, закрытые до этой
  модели. Их семантика неизвестна и **не угадывается** (§97: «мигрировать
  автоматически нельзя ни в ACHIEVED, ни в ARCHIVED») — до управляемой
  сверки. Миграция `0006` кладёт их сюда, а не в правдоподобное.

Бездействие, завершённая бронь и завершённый план сами по себе цель в
`ACHIEVED` не переводят (§97) — здесь нет ни одного автоматического
писателя терминальных состояний.

Инварианты:
- хотя бы одно из `goal_key` / `goal_text` заполнено (CheckConstraint);
- одна ACTIVE цель на клиента (partial UniqueConstraint). Это
  ограничение схемы, а не домена (§97 OD-GOAL-E допускает несколько
  ACTIVE); снимать его — отдельное решение, потому что двенадцать
  потребителей скаляра `known.goal` механически на список не переводятся.
  Пока оно стоит, смена цели закрывает прежний ряд как `SUPERSEDED`.
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

    class State(models.TextChoices):
        """Состояние цели — см. докстринг модуля; переходы в ``lifecycle.py``."""

        ACTIVE = "active", "Актуальна"
        PAUSED = "paused", "На паузе"
        ACHIEVED = "achieved", "Достигнута"
        ARCHIVED = "archived", "В архиве"
        SUPERSEDED = "superseded", "Закрыта выбором новой цели"
        LEGACY_INACTIVE_REASON_UNKNOWN = (
            "legacy_inactive_reason_unknown",
            "Закрыта до модели состояний; причина неизвестна",
        )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="client_goals",
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
    state = models.CharField(
        max_length=32,
        choices=State.choices,
        default=State.ACTIVE,
        help_text="Состояние жизненного цикла (§97 OD-GOAL-B); менять через goals.lifecycle",
    )
    # Когда цель перешла в текущее состояние. NULL — у строк, закрытых до
    # модели состояний: момент неизвестен, и выдумывать его нельзя по той
    # же причине, по которой не угадывается их состояние.
    state_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Момент последнего перехода состояния; NULL у legacy-строк",
    )
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
                condition=models.Q(state="active"),
                name="clientgoal_one_active_per_client",
            ),
        ]
        indexes = [
            models.Index(
                fields=["client", "state"],
                name="clientgoal_client_state_idx",
            ),
        ]

    def __str__(self) -> str:
        shown = self.goal_key or (self.goal_text or "")[:40]
        return f"ClientGoal<{self.client_id}> {shown} ({self.state})"


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
    # DRF-1746 — ответ шага в режиме ``multi``: ключи отмеченных вариантов
    # в порядке вариантов шага. Пустой список у остальных режимов.
    option_keys = models.JSONField(
        default=list,
        blank=True,
        help_text="Ключи отмеченных вариантов (режим multi); [] у остальных",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(option_key__isnull=False)
                    | models.Q(answer_text__isnull=False)
                    | ~models.Q(option_keys=[])
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
