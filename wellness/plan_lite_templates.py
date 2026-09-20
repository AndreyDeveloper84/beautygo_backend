"""Шаблоны Plan Lite по цели — таблица владельца §51 (DRF-2123, План-A).

Таблица — ДАННЫЕ (``wellness.PlanTemplate``), не код: сюда положен только
её исходный текст для сида (``seed_plan_templates``) и чтение активного
шаблона для предложения плана. Тексты ``why_text`` — константы данных,
дословно по решению владельца 19.09; тест
``wellness/tests/test_plan_templates_2123.py`` держит вторую копию и
сверяет обе.

«—» в таблице владельца = действие в шаблон не входит. ``book_service``
всегда 1 раз за ведро; категория для него не хранится — выводится из цели
на стороне подбора (``goals.wiring.goal_category_ids_for_key``).

Ни одного наблюдения тела — модуль под переписью импортов
``plan_lite*.py`` (DRF-2101).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Max

from .models import PlanTemplate


def _row(
    goal_key: str,
    book: str,
    food: int | None,
    water: int | None,
    why: str,
    hint: list[str] | None = None,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = [
        {"action_type": "book_service", "cadence": book, "target_count": 1},
    ]
    if food is not None:
        actions.append({"action_type": "log_food", "cadence": "per_week", "target_count": food})
    if water is not None:
        actions.append({"action_type": "log_water", "cadence": "per_day", "target_count": water})
    return {
        "goal_key": goal_key,
        "actions": actions,
        "why_text": why,
        "nutrition_goal_hint": list(hint or []),
    }


#: Таблица §51 дословно: goal_key · book_service · log_food/нед · log_water/день · почему ·
#: подсказка анкете питания (DRF-2124: body_shape → lose или maintain — выбор человека;
#: recharge / self_care → maintain; остальные — без подсказки).
PLAN_TEMPLATES_SEED: list[dict[str, Any]] = [
    _row(
        "body_shape", "per_week", 5, 6,
        "Фигура — это регулярность: процедура раз в неделю и еда, которую видно. "
        "Не обещаю килограммы — покажу, что получается",
        hint=["lose", "maintain"],
    ),
    _row(
        "event", "per_week", None, 5,
        "К дате лучше идти малыми шагами: процедура заранее, а не накануне, "
        "и вода — кожа скажет спасибо",
    ),
    _row(
        "new_look", "per_week", None, None,
        "Образ — это несколько визитов, не один. Начнём с первого, дальше подскажу по записям",
    ),
    _row(
        "recharge", "per_week", 3, 5,
        "Силы возвращают сон, вода и еда без пропусков — и час на себя раз в неделю",
        hint=["maintain"],
    ),
    _row(
        "relax", "per_week", None, 4,
        "Расслабление не копится про запас — раз в неделю час без телефона",
    ),
    _row(
        "self_care", "per_2_weeks", 3, 5,
        "Забота — это привычка, не рывок: маленькие регулярные шаги",
        hint=["maintain"],
    ),
    _row(
        "skin_care", "per_2_weeks", None, 6,
        "Кожа любит воду и регулярность процедур",
    ),
]


@dataclass(frozen=True)
class SeedReport:
    created: int
    deactivated: int
    unchanged: int


def seed_plan_templates(rows: list[dict[str, Any]] | None = None) -> SeedReport:
    """Идемпотентно положить таблицу: строка без изменений — пропуск;
    изменился текст, действия или подсказка анкете (DRF-2124) — новая версия,
    прежняя активная снимается. Ничего не удаляется: на версии ссылаются
    планы строкой ``source``. Строка с опечаткой в подсказке падает на
    ``full_clean`` до записи — весь сид откатывается."""
    rows = PLAN_TEMPLATES_SEED if rows is None else rows
    created = deactivated = unchanged = 0
    with transaction.atomic():
        for row in rows:
            goal_key = row["goal_key"]
            hint = list(row.get("nutrition_goal_hint") or [])
            active = PlanTemplate.objects.filter(goal_key=goal_key, is_active=True).first()
            if (
                active is not None
                and active.actions == row["actions"]
                and active.why_text == row["why_text"]
                and active.nutrition_goal_hint == hint
            ):
                unchanged += 1
                continue
            last = PlanTemplate.objects.filter(goal_key=goal_key).aggregate(m=Max("version"))["m"] or 0
            fresh = PlanTemplate(
                goal_key=goal_key,
                actions=row["actions"],
                why_text=row["why_text"],
                nutrition_goal_hint=hint,
                version=last + 1,
                is_active=True,
            )
            # Валидация полей до снятия прежней версии: опечатка в сиде не
            # должна оставить цель без активного шаблона. Уникальность и
            # частичный constraint здесь не проверяются — прежняя версия ещё
            # активна намеренно, её снимает следующая строка.
            fresh.full_clean(validate_unique=False, validate_constraints=False)
            if active is not None:
                active.is_active = False
                active.save(update_fields=["is_active"])
                deactivated += 1
            fresh.save()
            created += 1
    return SeedReport(created=created, deactivated=deactivated, unchanged=unchanged)


def active_template_for(goal_key: str | None) -> PlanTemplate | None:
    if not goal_key:
        return None
    return PlanTemplate.objects.filter(goal_key=goal_key, is_active=True).first()


def nutrition_goal_hint_for(goal_key: str | None) -> list[str] | None:
    """Подсказка анкете питания под курируемую цель (DRF-2124) — или ``None``,
    когда подсказки нет: шаблона нет или его подсказка пуста (§103 — нет входа,
    ``None``, не пустышка). Чтение данных, не решение: анкета подсвечивает,
    человек выбирает."""
    template = active_template_for(goal_key)
    if template is None or not template.nutrition_goal_hint:
        return None
    return list(template.nutrition_goal_hint)


def nutrition_goal_hints() -> dict[str, list[str]]:
    """Все непустые подсказки активных шаблонов одним запросом — для документов,
    которые печатают несколько целей разом (decision-context: ``known.goal`` +
    ``known.goals``). Ключа нет — подсказки нет."""
    return {
        t.goal_key: list(t.nutrition_goal_hint)
        for t in PlanTemplate.objects.filter(is_active=True).only("goal_key", "nutrition_goal_hint")
        if t.nutrition_goal_hint
    }


def template_version_exists(goal_key: str | None, version: int) -> bool:
    """Любая версия этой цели, активная или снятая: план, составленный по
    прежней версии, остаётся законно помеченным."""
    if not goal_key:
        return False
    return PlanTemplate.objects.filter(goal_key=goal_key, version=version).exists()
