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

DRF-1451 — версия 2: анкета
---------------------------

Решение владельца 03.09.2026 (поправка A-1 к BOT-001, §24): клиент без
цели получает **последовательность** вопросов, по завершении которой
цель сформирована. Последовательность целиком серверная — см.
``goals/anketa.py``; сюда она попадает по одному шагу за раз, готовым
элементом ``missing``. Экран по-прежнему не вычисляет ничего: он не
знает ни следующего вопроса, ни номера текущего — номер приходит в
``progress``.

Что версия 2 добавила к документу:

- ``missing[].kind == "goal_anketa"`` — шаг анкеты; у него, помимо
  прежних ``kind``/``prompt``, есть ``step``, ``options``,
  ``allow_free_text`` и ``progress``. Старые kind'ы не тронуты, поэтому
  потребитель, читающий только ``prompt``, продолжает работать.
- ``next`` — куда вести человека, когда спрашивать больше нечего.
  Раньше этого решения не было ни у кого: экран после выбора цели просто
  перерисовывался. Теперь его принимает сервер, а не клиент.
- намерение ``start_anketa`` — вход в анкету для того, кто УЖЕ с целью
  (DRF-1225 / C-4: проходить сколько угодно раз).

Анкета — не ворота (условие C-2). ``suggestions`` и ``formulate_own``
остаются в документе на каждом шаге: назвать услугу и уйти к подбору
можно, не ответив ни на один вопрос. И названная услуга **признаётся
готовой целью** — ``_goal_is_resolved`` ниже — чтобы не уронить человека
обратно в уточнение.

Флаг ``GOAL_ANKETA_ENABLED`` (умолчание ON) выключает анкету и
возвращает ровно документ DRF-1190.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db.models.functions import Lower

from services.models import GoalOption

from . import anketa
from .models import ClientGoal, GoalAnketaRun
from .service_match import match_named_service

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

INTENT_START_ANKETA = "start_anketa"

_INTENTS: list[dict[str, str]] = [
    {"id": INTENT_CHOOSE_SUGGESTED, "label": "Выбрать из предложенного"},
    {"id": INTENT_FORMULATE_OWN, "label": "Сформулирую своими словами"},
    {"id": INTENT_NEED_GUIDANCE, "label": "Не понимаю, чего хочу"},
]

# Намерение повторного прохода. Отдельно от ``_INTENTS``, потому что
# показывается не всегда: предлагать «пройти анкету заново» тому, кто
# сейчас в середине прохода, — предлагать бросить начатое.
INTENT_START_ANKETA_LABEL = "Пройти анкету заново"

# DRF-1451: куда вести, когда спрашивать больше нечего. Идентификатор, не
# путь: маршрут — контракт клиента (тот же приём, что у слагов бота в
# ``_ROUTE_MAP``), и сервер не обязан знать имена экранов мини-аппа.
NEXT_BROWSE_CATALOG = "browse_catalog"
NEXT_BROWSE_CATALOG_LABEL = "Найти услугу"

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


def _anketa_enabled() -> bool:
    return bool(getattr(settings, "GOAL_ANKETA_ENABLED", True))


def open_anketa_run(client: User) -> GoalAnketaRun | None:
    """Незакрытый проход анкеты клиента, если он есть."""
    return (
        GoalAnketaRun.objects.filter(client=client, completed_at__isnull=True)
        .order_by("-started_at")
        .first()
    )


def next_anketa_step(run: GoalAnketaRun | None) -> anketa.AnketaStep:
    """Какой шаг задавать сейчас. ``None`` — проход ещё не начат."""
    answered: set[str] = set()
    if run is not None:
        answered = set(run.answers.values_list("step_key", flat=True))
    return anketa.next_step(answered)


def _goal_is_resolved(goal: ClientGoal) -> bool:
    """Считается ли цель готовой — то есть уточнять больше нечего.

    Ключ курируемой подсказки готов всегда. Свободный текст готов, если
    в нём **названо** что-то известное: точное совпадение с label цели
    (прежняя семантика ``goals.resolution``) или имя услуги/категории из
    каталога (DRF-1451).

    Ради этой функции всё и затевалось: без неё человек, написавший
    «хочу маникюр», получал ``goal_clarification`` — то есть падал
    обратно в вопросы ровно там, где владелец велел вести к подбору.
    """
    if goal.goal_key:
        return True
    text = (goal.goal_text or "").strip()
    if not text:
        return False
    exact_label = (
        GoalOption.objects.filter(is_active=True)
        .annotate(label_lower=Lower("label"))
        .filter(label_lower=text.casefold())
        .exists()
    )
    if exact_label:
        return True
    return match_named_service(text) is not None


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

    anketa_on = _anketa_enabled()
    run = open_anketa_run(client) if anketa_on else None

    missing: list[dict[str, Any]] = []
    if guidance:
        missing.append({"kind": MISSING_GOAL_GUIDANCE, "prompt": PROMPT_GOAL_GUIDANCE})
    elif anketa_on and (run is not None or active_goal is None):
        # DRF-1451. Открытый проход ведём до конца независимо от того,
        # есть ли уже цель: повторный проход (C-4) начинается именно так.
        # Прохода нет и цели нет — задаём первый вопрос; строка прохода
        # появится на первом ответе, потому что GET не пишет в БД.
        missing.append(anketa.as_missing_item(next_anketa_step(run)))
    elif active_goal is None:
        missing.append({"kind": MISSING_GOAL, "prompt": PROMPT_GOAL_MISSING})
    elif not _goal_is_resolved(active_goal):
        # Свободный текст, в котором ничего не названо: уверенность
        # низкая — уточняем, а не отображаем насильно в ближайший чип
        # (OD-1). Названная услуга сюда не попадает (DRF-1451).
        missing.append({
            "kind": MISSING_GOAL_CLARIFICATION,
            "prompt": PROMPT_GOAL_CLARIFICATION.format(
                goal_text=(active_goal.goal_text or "")[:200],
            ),
        })

    intents = list(_INTENTS)
    if anketa_on and run is None and active_goal is not None:
        # Повторный проход предлагаем только тому, кто уже с целью и
        # сейчас не в середине анкеты (DRF-1225 / C-4).
        intents.append(
            {"id": INTENT_START_ANKETA, "label": INTENT_START_ANKETA_LABEL}
        )

    # Спрашивать больше нечего и цель есть — сервер называет следующий
    # шаг сам. Формулировка нарочно ничего не обещает про подбор под
    # цель: GOAL_RESOLUTION_ENABLED на пилоте выключен, и обещание было
    # бы ложью до его включения.
    next_step_hint: dict[str, str] | None = None
    if not missing and active_goal is not None:
        next_step_hint = {
            "id": NEXT_BROWSE_CATALOG,
            "label": NEXT_BROWSE_CATALOG_LABEL,
        }

    return {
        "version": 2,
        "known": known,
        "missing": missing,
        "suggestions": _suggestions(),
        "intents": intents,
        "next": next_step_hint,
    }
