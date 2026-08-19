"""Минимальный Unified DecisionContext — эфемерный документ состояния.

Ответ 3 главного окна (2026-08-19): экран — тупой отрисовщик. Он получает
документ «что известно / чего не хватает / что предложить» и только
отображает его; ни одного решения на клиенте. Когда появится Decision
Orchestrator, меняется этот модуль (и слой над ним), экран не трогаем.

Инвариант контракта: **документ не содержит данных, из которых экран мог
бы вычислить другое содержимое** — ни флагов «показать X», ни сырых
списков, требующих фильтрации. Всё, что приходит, отображается как есть.

Документ эфемерен: в БД не хранится. Durable-факт — goals.ClientGoal,
проекция строится поверх него на каждый запрос.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.models import GoalOption

from .models import ClientGoal

if TYPE_CHECKING:
    from users.models import User

# Тексты — часть серверного контракта, не логика экрана (Ответ 3).
PROMPT_GOAL_MISSING = "Что хочешь изменить или как хочешь себя чувствовать?"
PROMPT_GOAL_CLARIFICATION = (
    "Записала: «{goal_text}». Расскажи чуть подробнее — "
    "что для тебя важнее всего?"
)
PROMPT_GOAL_GUIDANCE = (
    "Давай разберёмся вместе. Что сейчас беспокоит больше всего — "
    "тело, внешность, состояние?"
)

# Три намерения DRF-1190 — данными, чтобы экран не решал даже их состав.
INTENT_CHOOSE_SUGGESTED = "choose_suggested"
INTENT_FORMULATE_OWN = "formulate_own"
INTENT_NEED_GUIDANCE = "need_guidance"

_INTENTS: list[dict[str, str]] = [
    {"id": INTENT_CHOOSE_SUGGESTED, "label": "Выбрать из предложенного"},
    {"id": INTENT_FORMULATE_OWN, "label": "Сформулирую своими словами"},
    {"id": INTENT_NEED_GUIDANCE, "label": "Не понимаю, чего хочу"},
]

MISSING_GOAL = "goal"
MISSING_GOAL_CLARIFICATION = "goal_clarification"
MISSING_GOAL_GUIDANCE = "goal_guidance"


def _goal_payload(goal: ClientGoal) -> dict[str, Any]:
    return {
        "goal_key": goal.goal_key,
        "goal_text": goal.goal_text,
        "selected_at": goal.selected_at.isoformat(),
        "source_channel": goal.source_channel,
    }


def _suggestions() -> list[dict[str, Any]]:
    """Активные курируемые подсказки — уже отфильтрованные и отсортированные."""
    return [
        {"key": option.key, "label": option.label}
        for option in GoalOption.objects.filter(is_active=True)
    ]


def build_decision_context(
    client: User,
    *,
    guidance: bool = False,
) -> dict[str, Any]:
    """Собрать документ состояния для клиента.

    ``guidance=True`` — ответ на намерение «не понимаю, чего хочу»:
    состояние ведения, в котором Ayla задаёт первый вопрос. Эфемерно
    (Ответ 3: ведущий сценарий — следующий проход; состояние не пишется
    в ClientGoal, потому что цели ещё нет).
    """
    active_goal = (
        ClientGoal.objects.filter(client=client, is_active=True)
        .order_by("-selected_at")
        .first()
    )

    known: dict[str, Any] = {"goal": _goal_payload(active_goal) if active_goal else None}

    missing: list[dict[str, str]] = []
    if guidance:
        missing.append({"kind": MISSING_GOAL_GUIDANCE, "prompt": PROMPT_GOAL_GUIDANCE})
    elif active_goal is None:
        missing.append({"kind": MISSING_GOAL, "prompt": PROMPT_GOAL_MISSING})
    elif active_goal.goal_key is None:
        # Свободный текст без ключа — уверенность низкая: уточняем,
        # а не отображаем насильно в ближайший чип (OD-1).
        missing.append({
            "kind": MISSING_GOAL_CLARIFICATION,
            "prompt": PROMPT_GOAL_CLARIFICATION.format(
                goal_text=(active_goal.goal_text or "")[:200],
            ),
        })

    return {
        "version": 1,
        "known": known,
        "missing": missing,
        "suggestions": _suggestions(),
        "intents": list(_INTENTS),
    }
