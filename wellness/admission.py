"""Допуск weight-loss Personal Plan по вердикту safety-лестницы (DRF-1340).

Чистая функция допуска: на входе — plain-вердикт профиля питания
(`NutritionVerdict`, frozen dataclass), на выходе — `AdmissionDecision`
(разрешение либо машиночитаемый код причины отказа), в стиле
`GateDecision` из `wellness/services.py`.

Границы:
- функция ничего не создаёт и не хранит; лестницу не вызывает —
  только читает уже готовый вердикт;
- модуль НЕ импортирует `nutrition.*` (nutrition про wellness не знает,
  и wellness про nutrition — тоже: связь через plain-данные);
- значение веса не читается вообще: сигнал про допущенный вес —
  только маркер в `assumed_inputs` сериализованного профиля (DRF-1339),
  ни значение, ни предикат на None здесь не появляются;
- текстов отказа нет — наружу только код.

Гейтинг применяется ТОЛЬКО к weight-loss (outcome_target=body_weight,
direction=reduce). Не-весовые цели функция не ограничивает — сигнатура
это выражает явными параметрами.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class OutcomeTarget:
    """Ключи объекта результата (соответствуют DesiredOutcome.target)."""

    BODY_WEIGHT = "body_weight"


class PlanDirection:
    """Направления изменения (соответствуют DesiredOutcome.Direction)."""

    REDUCE = "reduce"
    INCREASE = "increase"
    MAINTAIN = "maintain"


#: Отказы лестницы, при которых weight-loss Personal Plan не открывается.
SAFETY_OVERRIDE_REASONS = (
    "eating_disorder",
    "pregnancy",
    "breastfeeding",
    "bmr_floor",
)

#: Маркер допущенного (не указанного человеком) веса в `assumed_inputs`
#: сериализованного профиля питания (DRF-1339). Это единственное место
#: модуля, где маркер объявлен; значение веса здесь не читается.
ASSUMED_WEIGHT_MARKER = "weight_kg"


@dataclass(frozen=True)
class NutritionVerdict:
    """Plain-вход: вердикт safety-лестницы профиля питания.

    Не ORM-объект и не сериализатор — вызывающий код сам собирает его
    из ответа nutrition. `goal_overridden_by` пуст, когда цель выбрана
    человеком; непустое значение — коэрцированный лестницей `maintain`
    с кодом причины. `assumed_inputs` — маркеры допущенных входов.
    """

    goal: str
    goal_overridden_by: str = ""
    assumed_inputs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdmissionDecision:
    """Исход допуска. `reason_code` — машиночитаемый код отказа/пропуска."""

    allowed: bool
    reason_code: str


def personal_plan_admission(
    verdict: NutritionVerdict,
    *,
    outcome_target: str,
    direction: str,
) -> AdmissionDecision:
    """Допуск Personal Plan по вердикту лестницы; гейтит только weight-loss.

    Для (outcome_target=body_weight, direction=reduce):
    - `goal_overridden_by` из SAFETY_OVERRIDE_REASONS -> отказ с этим кодом
      (коэрцированный лестницей `maintain` отличается от выбранного
      человеком тем, что у выбранного `goal_overridden_by` пуст);
    - маркер допущенного веса в `assumed_inputs` -> отказ `assumed_weight`,
      даже если `goal == "lose"`;
    - иначе допуск (`admitted`).

    Любая другая пара (outcome_target, direction) не ограничивается:
    вердикт не консультируется, ответ `non_weight_loss_target`.
    """
    if (
        outcome_target != OutcomeTarget.BODY_WEIGHT
        or direction != PlanDirection.REDUCE
    ):
        return AdmissionDecision(allowed=True, reason_code="non_weight_loss_target")
    if verdict.goal_overridden_by in SAFETY_OVERRIDE_REASONS:
        return AdmissionDecision(allowed=False, reason_code=verdict.goal_overridden_by)
    if ASSUMED_WEIGHT_MARKER in verdict.assumed_inputs:
        return AdmissionDecision(allowed=False, reason_code="assumed_weight")
    return AdmissionDecision(allowed=True, reason_code="admitted")
