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


def direction_for(goal: ClientGoal, answers: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Направление под цель: строка (цель × область), иначе (цель, ''), иначе ``None``.

    Цель без ключа (свободный текст) направления не имеет — курируемой
    строки под неё быть не может; это честный ``None``, а не подбор по
    близости (OD-1).
    """
    if not goal.goal_key:
        return None
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
            goal_option__key=goal.goal_key,
            is_active=True,
            area_key__in=[area_key, ""] if area_key else [""],
        ).order_by("sort_order")
    )
    chosen = next((r for r in rows if r.area_key == area_key), None) if area_key else None
    if chosen is None:
        chosen = next((r for r in rows if r.area_key == ""), None)
    if chosen is None:
        return None
    return {
        "what": chosen.what,
        "subline": chosen.subline,
        "area_key": chosen.area_key or None,
    }
