"""Переходы состояния цели (DRF-1660; §97 OD-GOAL-B, основание запрета — §122).

Единственный писатель ``ClientGoal.state`` после создания строки.
Правило одно: **из каждого нетерминального состояния есть выход, и
ни один выход не требует замещающей цели**. Прежний ``is_active`` этого
не давал — его единственный писатель сидел внутри «записать новую,
закрыв прежнюю».

Таблица переходов::

    ACTIVE   → PAUSED · ACHIEVED · ARCHIVED
    PAUSED   → ACTIVE · ACHIEVED · ARCHIVED
    ACHIEVED, ARCHIVED, SUPERSEDED, LEGACY_INACTIVE_REASON_UNKNOWN — терминальны

Терминальность трёх последних — не §122-ловушка: ``ACHIEVED`` и
``ARCHIVED`` — слово человека, ``SUPERSEDED`` — он уже назвал новую цель.
Выход из них один и он существует: выбрать цель заново (новая строка,
старая остаётся историей). Возвращать архивную цель в ``ACTIVE`` владелец
не постановлял; когда постановит — это одна строка в таблице выше.

``LEGACY_INACTIVE_REASON_UNKNOWN`` терминален по другой причине: его
семантика неизвестна, и переводить его куда-либо кодом значило бы
угадать её — §97 это запрещает до управляемой сверки.

Два отказа, каждый со своим адресом починки:

- ``GOAL_TRANSITION_NOT_ALLOWED`` — перехода нет в таблице (чинится
  выбором другого действия или новой цели);
- ``GOAL_ANOTHER_ACTIVE`` — снять с паузы нельзя, пока у клиента есть
  другая ``ACTIVE`` цель: схема держит «одна ACTIVE на клиента»
  (ограничение схемы, не домена — см. докстринг модели). Чинится
  паузой/архивом той, другой. Тихо закрыть её здесь было бы хуже
  отказа: человек снимал с паузы одну цель, а потерял бы другую.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ClientGoal

logger = logging.getLogger(__name__)

State = ClientGoal.State

#: Куда можно перейти из каждого состояния. Пустое множество — терминал.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    State.ACTIVE: frozenset({State.PAUSED, State.ACHIEVED, State.ARCHIVED}),
    State.PAUSED: frozenset({State.ACTIVE, State.ACHIEVED, State.ARCHIVED}),
    State.ACHIEVED: frozenset(),
    State.ARCHIVED: frozenset(),
    State.SUPERSEDED: frozenset(),
    State.LEGACY_INACTIVE_REASON_UNKNOWN: frozenset(),
}

#: Состояния, которые человек вправе запросить сам. ``SUPERSEDED`` и
#: ``LEGACY_*`` сюда не входят: первое пишет только выбор новой цели,
#: второе — только миграция.
REQUESTABLE_STATES: frozenset[str] = frozenset(
    {State.ACTIVE, State.PAUSED, State.ACHIEVED, State.ARCHIVED}
)

#: Состояния, в которых цель «ещё моя» и должна быть видна человеку в
#: документе состояния, чтобы у неё был выход (§122).
OPEN_STATES: frozenset[str] = frozenset({State.ACTIVE, State.PAUSED})

GOAL_TRANSITION_NOT_ALLOWED = "GOAL_TRANSITION_NOT_ALLOWED"
GOAL_ANOTHER_ACTIVE = "GOAL_ANOTHER_ACTIVE"


class GoalTransitionRefused(Exception):
    """Переход не выполнен; ``code`` — один из двух кодов выше."""

    def __init__(self, code: str, *, goal: ClientGoal, to_state: str) -> None:
        self.code = code
        self.from_state = goal.state
        self.to_state = to_state
        super().__init__(f"{code}: {goal.state} -> {to_state} (goal={goal.id})")


def transition(goal: ClientGoal, to_state: str) -> ClientGoal:
    """Перевести цель в ``to_state`` или отказать с названной причиной.

    Идемпотентно для ``to_state == goal.state``: повторный тап «на паузу»
    по цели на паузе — не ошибка и не второй переход (``state_changed_at``
    не сдвигается).
    """
    if to_state == goal.state:
        return goal

    if to_state not in ALLOWED_TRANSITIONS.get(goal.state, frozenset()):
        raise GoalTransitionRefused(GOAL_TRANSITION_NOT_ALLOWED, goal=goal, to_state=to_state)

    from_state = goal.state
    now = timezone.now()
    try:
        with transaction.atomic():
            updated = ClientGoal.objects.filter(pk=goal.pk, state=from_state).update(
                state=to_state, state_changed_at=now,
            )
    except IntegrityError as exc:
        # Единственное ограничение, которое здесь может сработать, —
        # «одна ACTIVE на клиента» при снятии с паузы. Отказ, а не
        # закрытие соседа: см. докстринг модуля.
        if to_state == State.ACTIVE:
            raise GoalTransitionRefused(GOAL_ANOTHER_ACTIVE, goal=goal, to_state=to_state) from exc
        raise
    if updated == 0:
        # Строка ушла из ``from_state`` между чтением и записью: другой
        # вызывающий (бот и мини-апп — два независимых) успел раньше.
        # Перечитать и отказать честно, а не перезаписать его переход.
        goal.refresh_from_db(fields=["state", "state_changed_at"])
        raise GoalTransitionRefused(GOAL_TRANSITION_NOT_ALLOWED, goal=goal, to_state=to_state)

    goal.state = to_state
    goal.state_changed_at = now
    logger.info(
        "goal.state_changed client_id=%s goal_id=%s %s->%s",
        goal.client_id, goal.id, from_state, to_state,
    )
    return goal


def supersede_active(client) -> int:
    """Закрыть ACTIVE цель клиента выбором новой — писатель ``SUPERSEDED``.

    Вызывается только из ``_create_goal``; человек ничего не «архивирует»,
    он назвал новую цель, и это ровно то, что записано. ``PAUSED`` цели не
    трогаются: они не мешают ограничению схемы и остаются на паузе.
    """
    return ClientGoal.objects.filter(client=client, state=State.ACTIVE).update(
        state=State.SUPERSEDED, state_changed_at=timezone.now(),
    )
