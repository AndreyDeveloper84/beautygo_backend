"""Направление под цель (WHAT карточки C04) и собранные ответы — DRF-1772 (К-3).

Что это
-------
Решение владельца §60: карточка C04 «направление + почему» строится для
пилота; OD_C04 §3 («направление — не runtime-объект») отменён в этой части.
Источник направления — курируемая таблица ``services.GoalDirection``
(данные владельца, как ``GoalOption``): ключ — цель × область (ответ шага
``area`` анкеты), запасная строка — под цель целиком.

Чего здесь нет
--------------
Ни одной фразы, придуманной кодом: таблица пустая → ``None`` → карточки нет
(бот показывает C04.4 механически, OD_C04 §2). Ни услуги, ни мастера, ни
цены: направление — WHAT (B2/B3), исполнение — C05.

Почему ответы едут рядом с направлением
---------------------------------------
WHY карточки — grounded-пересказ того, что человек сказал в этом пути
(OD_C04 §1). ``known.anketa`` документа несёт ответы только ОТКРЫТОГО
прохода (DRF-1744 — блок «Уже учла» во время вопросов); после последнего
ответа проход закрыт, и ответы под цель живут в его завершённом проходе.
Отсюда ``answers_for_goal``: те же строки в той же форме, что «Уже учла»,
но из завершённого прохода этой цели — бот собирает из них причины, не
сочиняя ничего.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from services.models import GoalDirection

from . import anketa
from .models import GoalAnketaAnswer

if TYPE_CHECKING:
    from .models import ClientGoal


def answers_for_goal(goal: ClientGoal) -> list[dict[str, Any]]:
    """Ответы на сужающие шаги из завершённого прохода ЭТОЙ цели — в
    порядке шагов, в форме «Уже учла» (`anketa.as_known_answer`).

    Последний завершённый проход цели; повторный проход перезаписывает
    картину целиком — человек ответил заново. «Не знаю» остаётся строкой
    (``unknown: true``): это ответ, но не факт, из которого можно сделать
    причину, — решает потребитель.
    """
    rows = (
        GoalAnketaAnswer.objects.filter(run__goal=goal, run__completed_at__isnull=False)
        .order_by("-run__completed_at", "-created_at")
    )
    by_step: dict[str, GoalAnketaAnswer] = {}
    latest_run_id = None
    for row in rows:
        if latest_run_id is None:
            latest_run_id = row.run_id
        if row.run_id != latest_run_id:
            break
        if anketa.narrowing_step(row.step_key) is None or row.step_key in by_step:
            continue
        by_step[row.step_key] = row
    return [
        anketa.as_known_answer(
            step,
            option_key=by_step[step.key].option_key,
            text=by_step[step.key].answer_text,
            option_keys=list(by_step[step.key].option_keys or []),
        )
        for step in anketa.ANKETA_STEPS
        if step.key in by_step
    ]


#: Сколько направлений едет в документ: основной + не больше двух других
#: (макет C04.2, R05 «≤2 meaningful alternatives»). Третьего нет — и это
#: предел ВЫДАЧИ, а не данных: строк под цель может быть больше.
MAX_DIRECTIONS = 3


def _as_payload(row: GoalDirection) -> dict[str, Any]:
    return {
        "what": row.what,
        "subline": row.subline,
        "area_key": row.area_key or None,
    }


def directions_for(
    goal: ClientGoal, answers: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Основной вариант и до двух других подходов — курируемые строки под цель.

    Порядок: основной первым (строка под названную область, иначе
    запасная под цель), затем остальные по ``sort_order``. Альтернативы —
    ТЕ ЖЕ строки, ничего не придумывается: пустая таблица даёт пустой
    список, как и отсутствие направления (OD_C04 §2 у потребителя).

    Цель свободным текстом направлений не имеет — курируемой строки под
    неё быть не может, и подбор по близости запрещён (OD-1).
    """
    if not goal.goal_key:
        return []
    if answers is None:
        answers = answers_for_goal(goal)
    area_key = next(
        (
            str(a.get("option_key") or "")
            for a in answers
            if a.get("step") == "area" and not a.get("unknown")
        ),
        "",
    )
    rows = list(
        GoalDirection.objects.filter(
            goal_option__key=goal.goal_key, is_active=True
        ).order_by("sort_order", "area_key")
    )
    if not rows:
        return []
    primary = next((r for r in rows if r.area_key == area_key), None) if area_key else None
    if primary is None:
        primary = next((r for r in rows if r.area_key == ""), None)
    if primary is None:
        # Под названную область строки нет и запасной нет: показывать
        # «другие подходы» без основного нечестно — это не выбор, а
        # подмена того, о чём человек сказал.
        return []
    others = [r for r in rows if r.pk != primary.pk]
    return [_as_payload(r) for r in [primary, *others][:MAX_DIRECTIONS]]


def direction_for(
    goal: ClientGoal, answers: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Основное направление под цель или ``None``.

    Остаётся рядом с :func:`directions_for` намеренно: потребитель, не
    знающий про альтернативы (бот до выкладки N4), продолжает читать одно
    поле и работать как раньше.
    """
    directions = directions_for(goal, answers)
    return directions[0] if directions else None
