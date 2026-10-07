"""Отзыв согласия ``health`` — удаление сведений о здоровье ОДНОГО человека (DRF-2776).

Решение владельца 05.10 (D-1, DRF-2434): при отзыве согласия ``health``
данные, собранные под ним, удаляются. В каталоге они живут в
``NutritionProfile`` — и не в одном поле.

### Что стирается

* **Флаги здоровья** в ``health_flags`` — беременность, вскармливание,
  расстройство пищевого поведения и всё, что не названо ниже как ответ о
  питании. Список неизвестного считается здоровьем: fail-closed.
* **Весь ориентир по калориям** (``targets_state.KIND_FIELDS["calories"]``) и
  его подпись (``calories_source = none``, ``calories_confirmed_at = NULL``).
  Флаги здоровья решают, считается ли ориентир вообще (фактор здоровья —
  отказ с именем), и поднимают микронутриенты: при беременности железо 27 мг.
  Стереть флаг и оставить число — значит оставить беременность записанной
  числом. Стереть только микронутриенты нельзя: вид остался бы «посчитан и
  подтверждён» с пустыми полями, и следующий расчёт ушёл бы в предложение
  рядом, а не на место. Ориентир по жидкости здоровьем не считается — он
  остаётся.
* **Предложение рядом** (``pending_proposal``) — оно несёт те же значения и
  вернуло бы их первым же подтверждением (тот же класс, что DRF-2192).
* **Причины отказа** ``health_factor_<имя>`` в ``last_overrides_applied`` —
  они называют состояние словами.

### Что НЕ трогается

Ответы о питании, которые лежат в том же JSON по историческим причинам:
``vegan``, ``vegetarian``, ``diet_preference_skipped`` (:data:`DIET_KEYS`) —
это не здоровье, и стереть пропуск значило бы задать вопрос о диете заново.
Параметры тела — у них своё согласие (``personal_calculation``). История
дневника — не данные здоровья.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from nutrition.models import NutritionProfile
from nutrition.services.diet_type import DIET_SKIPPED_FLAG
from nutrition.services.targets_state import (
    KIND_CALORIES,
    KIND_FIELDS,
    KIND_SOURCE_FIELD,
    KIND_STAMP_FIELD,
)

#: Ключи ``health_flags``, которые НЕ про здоровье: ответы о питании.
DIET_KEYS = frozenset({"vegan", "vegetarian", DIET_SKIPPED_FLAG})

#: Ориентир по калориям целиком — флаги здоровья решают его судьбу.
CALORIE_TARGETS: tuple[str, ...] = KIND_FIELDS[KIND_CALORIES]

#: Что стирается — имена полей, без значений.
ERASED_FIELDS: tuple[str, ...] = (
    "health_flags",
    *CALORIE_TARGETS,
    KIND_SOURCE_FIELD[KIND_CALORIES],
    KIND_STAMP_FIELD[KIND_CALORIES],
    "pending_proposal",
    "last_overrides_applied",
)

_HEALTH_REASON_PREFIX = "health_factor_"


class IncompleteErasure(RuntimeError):
    """После записи в базе не то, что объявлено, — откат целиком."""


@dataclass(frozen=True)
class HealthWithdrawalOutcome:
    erased: tuple[str, ...]
    profile_existed: bool


def _health_reasons(overrides) -> list:
    return [
        item for item in (overrides or [])
        if isinstance(item, dict) and str(item.get("reason", "")).startswith(_HEALTH_REASON_PREFIX)
    ]


def erase_health_flags(user) -> HealthWithdrawalOutcome:
    """Стереть сведения о здоровье и всё, что от них посчитано. Идемпотентно.

    Нет профиля — нечего стирать, и это не ошибка.
    """
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is None:
        return HealthWithdrawalOutcome(erased=(), profile_existed=False)

    with transaction.atomic():
        p = NutritionProfile.objects.select_for_update().get(pk=profile.pk)
        p.health_flags = {k: v for k, v in (p.health_flags or {}).items() if k in DIET_KEYS}
        for field in CALORIE_TARGETS:
            setattr(p, field, None)
        setattr(p, KIND_SOURCE_FIELD[KIND_CALORIES], NutritionProfile.TargetsSource.NONE)
        setattr(p, KIND_STAMP_FIELD[KIND_CALORIES], None)
        p.pending_proposal = None
        p.last_overrides_applied = [
            item for item in (p.last_overrides_applied or []) if item not in _health_reasons(p.last_overrides_applied)
        ]
        p.save(update_fields=[*ERASED_FIELDS, "updated_at"])

        p.refresh_from_db()
        residual = [k for k in (p.health_flags or {}) if k not in DIET_KEYS]
        residual += [f for f in CALORIE_TARGETS if getattr(p, f) is not None]
        if p.pending_proposal is not None:
            residual.append("pending_proposal")
        if _health_reasons(p.last_overrides_applied):
            residual.append("last_overrides_applied")
        if residual:
            raise IncompleteErasure(f"left={residual}")

    return HealthWithdrawalOutcome(erased=ERASED_FIELDS, profile_existed=True)
