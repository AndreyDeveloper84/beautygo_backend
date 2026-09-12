"""Отзыв согласия на персональный расчёт — удаление параметров ОДНОГО
человека (пакет решений владельца 12.09 §2, DRF-1698).

Владелец: после подтверждения «Отключить и удалить» —

* параметры перестают использоваться;
* параметры удаляются согласно контракту;
* derived personal norms инвалидируются / становятся недоступны;
* история Food Diary сохраняется.

### Что именно стирается — и почему шесть, а не четыре

§144 очертил объём ``purge_unconsented_body_parameters`` четырьмя столбцами
(вес, рост, возраст, пол) и велел расширять только решением владельца.
§2 от 12.09 — это решение: согласие покрывает **шесть** параметров (вес,
рост, возраст, пол для расчёта, уровень активности, цель), и после отзыва
удаляются «параметры». Активность и цель возвращаются к умолчаниям модели
(``activity_coefficient=1.4``, ``goal=""``) — у них нет NULL, «пусто» у
каждого поля выражается своим значением (тот же приём, что
``EMPTY_VALUE_BY_FIELD`` в команде §144).

### Порядок внутри одной транзакции — сначала ориентиры

Команда §144 отказывает, если у строки ещё есть ориентиры с
происхождением: стереть входы и оставить число значило бы «ориентир без
происхождения» (§92 правило 5). Здесь то же правило исполняется не
очерёдностью команд, а порядком в транзакции: ориентиры → NULL и
``targets_source=none`` (как ``clear_targets_without_provenance``), затем
входы, затем проверка полноты по перечитанной строке. Неполное удаление
откатывается целиком.

### Что НЕ трогается

``FoodLog`` / ``WaterEntry`` / ``FoodScan`` — история дневника; ``timezone``,
``diet_preference``, ``health_flags``, ``pace`` — не параметры расчёта по §2.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from nutrition.management.commands.clear_targets_without_provenance import (
    PROVENANCE_FIELDS,
    TARGET_FIELDS,
)
from nutrition.management.commands.purge_unconsented_body_parameters import (
    EMPTY_VALUE_BY_FIELD,
    PURGED_FIELDS,
    _names_left_in,
    _strip_purged,
)
from nutrition.models import NutritionProfile

#: Шесть параметров §2 = четыре столбца §144 + активность и цель.
WITHDRAWN_FIELDS: tuple[str, ...] = (*PURGED_FIELDS, "activity_coefficient", "goal")

_EMPTY_BY_FIELD = {
    **EMPTY_VALUE_BY_FIELD,
    "activity_coefficient": NutritionProfile._meta.get_field("activity_coefficient").default,
    "goal": "",
}


class IncompleteErasure(RuntimeError):
    """После записи в базе не то, что объявлено, — откат целиком."""


@dataclass(frozen=True)
class WithdrawalOutcome:
    erased: tuple[str, ...]
    targets_cleared: bool
    profile_existed: bool


def erase_personal_calculation_inputs(user) -> WithdrawalOutcome:
    """Стереть шесть параметров и ориентиры человека. Идемпотентно.

    Нет профиля — нечего стирать, и это не ошибка: человек мог отозвать
    согласие, не дойдя до анкеты.
    """
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is None:
        return WithdrawalOutcome(erased=(), targets_cleared=False, profile_existed=False)

    with transaction.atomic():
        p = NutritionProfile.objects.select_for_update().get(pk=profile.pk)
        had_targets = any(getattr(p, f) is not None for f in TARGET_FIELDS)

        # 1. Ориентиры — раньше входов (§92 п.5).
        for f in TARGET_FIELDS:
            setattr(p, f, None)
        p.targets_source = NutritionProfile.TargetsSource.NONE
        p.targets_method_versions = {}
        p.targets_input_snapshot = {}
        p.targets_computed_at = None

        # 2. Входы.
        for f in WITHDRAWN_FIELDS:
            setattr(p, f, _EMPTY_BY_FIELD[f])
        p.targets_input_snapshot = _strip_purged(p.targets_input_snapshot)

        p.save(
            update_fields=[*TARGET_FIELDS, *PROVENANCE_FIELDS, *WITHDRAWN_FIELDS, "updated_at"]
        )

        # 3. Полнота — по перечитанной строке, внутри транзакции.
        p.refresh_from_db()
        residual_targets = [f for f in TARGET_FIELDS if getattr(p, f) is not None]
        residual_inputs = [f for f in WITHDRAWN_FIELDS if getattr(p, f) != _EMPTY_BY_FIELD[f]]
        left_in_snapshot = _names_left_in(p.targets_input_snapshot)
        if residual_targets or residual_inputs or left_in_snapshot:
            raise IncompleteErasure(
                f"targets={residual_targets} inputs={residual_inputs} snapshot={left_in_snapshot}"
            )

    return WithdrawalOutcome(
        erased=WITHDRAWN_FIELDS, targets_cleared=had_targets, profile_existed=True
    )
