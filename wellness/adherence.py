"""Plan Adherence — сопоставление плановых обязательств с фактами (DRF-1334).

Контракт: docs/DRAFT_ADHERENCE_CONTRACT.md (вердикт главного окна 25.08
внесён: счёт по вёдрам каденса, §4).

- **Производная, не сущность.** Ничего не хранит; пересчитывается на вызов.
- **Счёт по ведру каденса, не по периоду.** `per_day` — про распределение:
  семь записей в один день при недельном окне — это 1/7, не 7/7. Арифметика,
  не суждение: ни мнения о времени суток, ни оценки человека.
- **Две линии не сходятся (AYLA-DEC-0082).** Здесь нет и не может быть
  поля, куда входит Outcome Progress; модуль не импортирует
  `wellness.progress`.
- Результат — структура по каждому действию, **без общего итога**: нет
  процента, суммы или цвета по плану в целом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from .fact_providers import count_facts
from .models import PlanAction

if TYPE_CHECKING:
    from .models import PersonalPlan

_CADENCE_BUCKET_DAYS = {
    PlanAction.Cadence.PER_DAY: 1,
    PlanAction.Cadence.PER_WEEK: 7,
}


@dataclass(frozen=True)
class ActionAdherence:
    """Сопоставление одного обязательства с фактами за период.

    `fulfilled_count` — сумма по вёдрам min(фактов в ведре, target_count).
    `target_total` — target_count × число вёдер. Никакого итога по плану —
    план не сводится в одно число.
    """

    action_type: str
    cadence: str
    target_total: int
    fulfilled_count: int


def _buckets(cadence: str, start: date, end: date) -> list[tuple[datetime, datetime]]:
    """Вёдра каденса внутри [start, end): день для per_day, 7 дней для
    per_week. Неполный хвост периода не считается (сознательно: метрика
    существует для полных единиц обязательства)."""
    step = _CADENCE_BUCKET_DAYS[cadence]
    buckets: list[tuple[datetime, datetime]] = []
    current = start
    while current + timedelta(days=step) <= end:
        bucket_end = current + timedelta(days=step)
        buckets.append((
            timezone.make_aware(datetime.combine(current, time.min)),
            timezone.make_aware(datetime.combine(bucket_end, time.min)),
        ))
        current = bucket_end
    return buckets


def compute_plan_adherence(
    plan: PersonalPlan,
    start: date,
    end: date,
) -> list[ActionAdherence]:
    """Adherence плана за [start, end) — по каждому PlanAction отдельно.

    Нет обязательства — нет и сопоставления: факт без PlanAction не растит
    adherence ни на сколько (приёмочный случай задачи).
    """
    result: list[ActionAdherence] = []
    for action in plan.actions.all():
        buckets = _buckets(action.cadence, start, end)
        fulfilled = sum(
            min(
                count_facts(action.action_type, plan.user_id, b_start, b_end),
                action.target_count,
            )
            for b_start, b_end in buckets
        )
        result.append(ActionAdherence(
            action_type=action.action_type,
            cadence=action.cadence,
            target_total=action.target_count * len(buckets),
            fulfilled_count=fulfilled,
        ))
    return result
