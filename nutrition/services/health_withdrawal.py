"""Отзыв согласия ``health`` — удаление флагов здоровья ОДНОГО человека (DRF-2776).

Решение владельца 05.10 (D-1, DRF-2434): при отзыве согласия ``health``
данные, собранные под ним, удаляются. В каталоге под ним живут
``NutritionProfile.health_flags`` — беременность, грудное вскармливание и
другие состояния, которые анкета питания спрашивает.

### Почему вместе с флагами стираются микронутриентные ориентиры

``nutrition_profile_service.compute_rda`` поднимает их от флагов: при
беременности железо 27 мг и омега-3 1.4 г, при вскармливании железо 9 мг и
кальций 1000 мг. Стереть флаг и оставить ``daily_iron_mg = 27`` — значит
оставить беременность записанной числом. Решение fail-closed — главное окно,
05.10.

Калории и жидкость флагами здоровья не считаются — они остаются; их
провенанс (``targets_source``, снимок входов) не трогается.

### Что НЕ трогается

Параметры тела — у них своё согласие (``personal_calculation``) и своя
функция (:mod:`nutrition.services.personal_calculation_withdrawal`). История
дневника (``FoodLog`` и др.) — не данные здоровья.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from nutrition.models import NutritionProfile

#: Ориентиры, которые ``compute_rda`` поднимает от флагов здоровья.
HEALTH_DEPENDENT_TARGETS: tuple[str, ...] = (
    "daily_vitamin_d_iu",
    "daily_vitamin_b12_mcg",
    "daily_vitamin_c_mg",
    "daily_iron_mg",
    "daily_calcium_mg",
    "daily_magnesium_mg",
    "daily_omega3_g",
    "daily_fiber_g",
)

#: Что стирается — имена полей, без значений: их возвращает квитанция.
ERASED_FIELDS: tuple[str, ...] = ("health_flags", *HEALTH_DEPENDENT_TARGETS)


class IncompleteErasure(RuntimeError):
    """После записи в базе не то, что объявлено, — откат целиком."""


@dataclass(frozen=True)
class HealthWithdrawalOutcome:
    erased: tuple[str, ...]
    profile_existed: bool


def erase_health_flags(user) -> HealthWithdrawalOutcome:
    """Стереть флаги здоровья и ориентиры, поднятые от них. Идемпотентно.

    Нет профиля — нечего стирать, и это не ошибка.
    """
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is None:
        return HealthWithdrawalOutcome(erased=(), profile_existed=False)

    with transaction.atomic():
        p = NutritionProfile.objects.select_for_update().get(pk=profile.pk)
        p.health_flags = {}
        for field in HEALTH_DEPENDENT_TARGETS:
            setattr(p, field, None)
        p.save(update_fields=[*ERASED_FIELDS, "updated_at"])

        p.refresh_from_db()
        residual = [f for f in HEALTH_DEPENDENT_TARGETS if getattr(p, f) is not None]
        if p.health_flags:
            residual = ["health_flags", *residual]
        if residual:
            raise IncompleteErasure(f"left={residual}")

    return HealthWithdrawalOutcome(erased=ERASED_FIELDS, profile_existed=True)
