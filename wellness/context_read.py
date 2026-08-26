"""Internal wellness-context read для решающего слоя бота (DRF-1344).

Эфемерный документ состояния wellness — вход Decision Policy, НЕ экран
и НЕ сообщение человеку. Контракт — «тупой потребитель»: документ
содержит ТОЛЬКО коды состояний и не содержит данных, из которых
вызывающий мог бы вычислить другое содержимое:

- **ни одного значения наблюдения** — ни `value_numeric`, ни
  `value_ordinal`, ни baseline/latest/delta. Это структурно: сериализатор
  берёт из `OutcomeProgress` только `.state`, полей со значениями в
  документе просто нет;
- **ни текстов** — ни `statement_text`, ни промптов;
- `horizon_status` — вычислимое property `PlanOutcomeLink` (§6), не
  колонка и не сырой `target_date`, из которого потребитель вычислял бы
  сам.

Fail-closed (GOALS-R5/R6): документ — это processing. Пока гейты
закрыты (Gate D до scope `goal_memory`, Gate O до Privacy/Legal —
см. `wellness/services.py`), endpoint возвращает честное состояние:
пустые проекции + коды причин из самих гейтов (не хардкод). Открытие
гейтов — отдельное решение; этот модуль менять не придётся: те же поля
наполнятся, а запрета на значения наблюдений это не касается — он
структурный.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import DesiredOutcome, PersonalPlan, PlanOutcomeLink
from .progress import compute_outcome_progress
from .services import Purpose, body_observation_gate, goal_intention_gate

if TYPE_CHECKING:
    from users.models import User


def _plan_payload(user: User) -> dict[str, Any] | None:
    """Активный Personal Plan — только код статуса (0..1 ACTIVE, OD-GOAL-4)."""
    plan = PersonalPlan.objects.filter(
        user=user, status=PersonalPlan.Status.ACTIVE,
    ).first()
    return {"status": plan.status} if plan else None


def _outcome_payload(outcome: DesiredOutcome) -> dict[str, Any]:
    """Коды состояний результата. Значений наблюдений здесь нет и не
    может быть: из `OutcomeProgress` читается только `.state`."""
    link = PlanOutcomeLink.objects.filter(
        outcome=outcome, status=PlanOutcomeLink.Status.ACTIVE,
    ).first()
    return {
        "target": outcome.target,
        "link_status": link.status if link else None,
        "horizon_status": link.horizon_status if link else None,
        "progress_state": compute_outcome_progress(outcome).state,
    }


def build_wellness_context(user: User) -> dict[str, Any]:
    """Собрать документ wellness-context для человека.

    Гейты консультируются с `purpose=processing` (amendment C): это
    product processing, не права субъекта. Закрытый гейт — не ошибка, а
    честное состояние документа.
    """
    gate_d = goal_intention_gate(None, purpose=Purpose.PROCESSING)
    gate_o = body_observation_gate(None, purpose=Purpose.PROCESSING)

    if not (gate_d.allowed and gate_o.allowed):
        return {
            "plan": None,
            "outcomes": [],
            "gated": {"gate_d": gate_d.reason_code, "gate_o": gate_o.reason_code},
        }

    outcomes = DesiredOutcome.objects.filter(
        user=user, status=DesiredOutcome.Status.OPEN,
    )
    return {
        "plan": _plan_payload(user),
        "outcomes": [_outcome_payload(outcome) for outcome in outcomes],
        "gated": None,
    }
