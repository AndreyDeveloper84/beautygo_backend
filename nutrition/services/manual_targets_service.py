"""Норма, заданная человеком сам (§5.1, 11.09.2026) — писатель ``user_entered``.

Решение владельца §5.1: «Пользователь может задать норму самостоятельно».
Источник ``user_entered`` был объявлен с N-d (#315), но писателя у него
не было ни для калорий, ни для воды: состояние существовало в словаре и
нигде не возникало. Здесь — единственный писатель.

### Что задаётся и что при этом стирается

Задать можно калории (``daily_kcal``) и воду (``daily_water_ml``) —
по одному или оба. Всё, что было ПОСЧИТАНО (белки/жиры/углеводы, RDA,
``bmr``), стирается: эти числа выведены из расчёта, который человек
только что заменил своим числом, и оставить их значило бы показать
макросы «от Ayla» рядом с калориями «от человека» под одним источником.
У ``targets_source`` одно значение на весь набор (§103), и оно честно
только тогда, когда весь набор одного происхождения. Раздельное
происхождение по видам — отдельное решение (реестр §150).

Ручная норма — подтверждена по построению (человек её и назвал):
``targets_confirmed_at`` = сейчас, снимок входов и версии методик пусты
(никакой методики не было — это и есть её отличие от ``ayla_calculated``).

### Пороги §85 (решение владельца 09.09.2026) — продуктовые, не медицинские

Калории:

* ``< 1000`` — жёсткий отказ, значение НЕ сохраняется
  (:class:`CaloriesBelowFloor`, 422);
* ``1000–1199`` — сохраняется с предупреждением ``calories_low``;
* отклонение от расчёта поддержания более чем на 30 % — неблокирующее
  подтверждение: без ``confirm_deviation=True`` — 409
  :class:`ConfirmationRequired` с числом поддержания; с ним —
  сохраняется. Поддержание считается ТОЛЬКО от снимка состоявшегося
  расчёта (``ayla_proposed`` / ``ayla_calculated``): выдумывать его от
  текущих полей профиля, собранных неизвестно когда, нельзя. Нет снимка
  — проверка не выполняется, и это НАЗВАНО в ответе
  (``deviation_check: "unavailable"``), а не выдано за «отклонения нет».

Вода:

* вне ``1000–5000`` мл/сутки — предупреждение и повторное подтверждение:
  без ``confirm_water_out_of_range=True`` — 409; с ним — сохраняется с
  предупреждением ``water_out_of_range``.

«Получил от специалиста» для воды (решение 09.09, раздел 4) здесь не
отдельная галочка: повторное подтверждение и есть тот ответ; текст
спрашивает бот. Для калорий ниже 1000 выхода через специалиста решение
не даёт — отказ без исключений (передача окна питания, замечание 4).
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz
from typing import Any

from django.db import transaction

from nutrition.models import NutritionProfile
from nutrition.services.targets_state import KIND_FIELDS, KIND_SOURCE_FIELD
from nutrition.services.nutrition_profile_service import (  # noqa: F401 — re-exported names
    CALORIES_HARD_FLOOR_KCAL,
    CALORIES_WARN_BELOW_KCAL,
    DEFAULT_ACTIVITY,
    MAINTENANCE_DEVIATION_RATIO,
    WARN_CALORIES_LOW,
    ProfileInputs,
    compute_norms,
)

# Пороги калорий — из ``nutrition_profile_service`` (DRF-2097): один источник
# для ручного значения и для расчёта; здесь — прежние имена для читателей.
#: §85 раздел 4: вне диапазона — предупреждение и повторное подтверждение.
WATER_MIN_ML = 1000
WATER_MAX_ML = 5000

#: Что стирается вместе с заменой расчёта ручной нормой. ``daily_kcal`` и
#: ``daily_water_ml`` сюда НЕ входят — их задаёт человек.
COMPUTED_FIELDS: tuple[str, ...] = (
    "bmr", "daily_protein_g", "daily_fat_g", "daily_carbs_g",
    "daily_vitamin_d_iu", "daily_vitamin_b12_mcg", "daily_vitamin_c_mg",
    "daily_iron_mg", "daily_calcium_mg", "daily_magnesium_mg",
    "daily_omega3_g", "daily_fiber_g",
)

#: Источники, у которых снимок входов — это входы СОСТОЯВШЕГОСЯ расчёта.
_SNAPSHOT_SOURCES = (
    NutritionProfile.TargetsSource.AYLA_PROPOSED,
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
)

WARN_WATER_OUT_OF_RANGE = "water_out_of_range"


class ManualTargetsError(Exception):
    code = "MANUAL_TARGETS_ERROR"
    details: dict[str, Any] = {}


class NothingToSet(ManualTargetsError):
    """Тело без калорий и без воды: задавать нечего."""

    code = "VALIDATION_ERROR"

    def __init__(self) -> None:
        self.details = {"fields": ["calories_kcal", "water_ml"]}
        super().__init__("Нужно задать калории или воду — хотя бы одно.")


class CaloriesBelowFloor(ManualTargetsError):
    """§85: ниже 1000 ккал — отказ, значение не сохраняется."""

    code = "CALORIES_BELOW_FLOOR"

    def __init__(self, value: int) -> None:
        self.details = {"calories_kcal": value, "floor_kcal": CALORIES_HARD_FLOOR_KCAL}
        super().__init__(
            f"Норма {value} ккал ниже порога {CALORIES_HARD_FLOOR_KCAL} — "
            "не сохраняется (§85)."
        )


class ConfirmationRequired(ManualTargetsError):
    """§85: значение сохраняется только после повторного подтверждения."""

    code = "CONFIRMATION_REQUIRED"

    def __init__(self, kind: str, details: dict[str, Any]) -> None:
        self.kind = kind
        self.details = {"kind": kind, **details}
        super().__init__(f"Нужно подтверждение: {kind}.")


def maintenance_kcal(profile: NutritionProfile) -> int | None:
    """Расчёт поддержания от снимка состоявшегося расчёта — или ``None``.

    Считается по той же методике (``compute_norms`` с целью ``maintain``)
    от ``targets_input_snapshot``, а не от текущих полей профиля: снимок —
    это входы, под которыми человек что-то видел; поля профиля могли
    смениться без расчёта. Нет снимка — нет поддержания; подставлять
    нечего.
    """
    if profile.targets_source not in _SNAPSHOT_SOURCES:
        return None
    snapshot = dict(profile.targets_input_snapshot or {})
    if not snapshot:
        return None
    norms = compute_norms(ProfileInputs(
        gender=str(snapshot.get("gender") or ""),
        age=snapshot.get("age"),
        height_cm=snapshot.get("height_cm"),
        weight_kg=snapshot.get("weight_kg"),
        activity_coefficient=float(snapshot.get("activity_coefficient") or DEFAULT_ACTIVITY),
        goal="maintain",
        pace="moderate",
        health_flags={},
    ))
    return norms.daily_kcal if norms.computed else None


def _check_calories(
    profile: NutritionProfile, value: int, *, confirm_deviation: bool,
) -> tuple[list[str], dict[str, Any]]:
    if value < CALORIES_HARD_FLOOR_KCAL:
        raise CaloriesBelowFloor(value)
    warnings: list[str] = []
    if value < CALORIES_WARN_BELOW_KCAL:
        warnings.append(WARN_CALORIES_LOW)

    maintenance = maintenance_kcal(profile)
    if maintenance is None:
        deviation: dict[str, Any] = {"deviation_check": "unavailable"}
    else:
        ratio = abs(value - maintenance) / maintenance
        deviation = {
            "deviation_check": "done",
            "maintenance_kcal": maintenance,
            "deviation_ratio": round(ratio, 3),
        }
        if ratio > MAINTENANCE_DEVIATION_RATIO and not confirm_deviation:
            raise ConfirmationRequired("calories_deviation", {
                "calories_kcal": value,
                "maintenance_kcal": maintenance,
                "deviation_ratio": round(ratio, 3),
                "limit_ratio": MAINTENANCE_DEVIATION_RATIO,
            })
    return warnings, deviation


def _check_water(value: int, *, confirm_out_of_range: bool) -> list[str]:
    if WATER_MIN_ML <= value <= WATER_MAX_ML:
        return []
    if not confirm_out_of_range:
        raise ConfirmationRequired("water_out_of_range", {
            "water_ml": value, "min_ml": WATER_MIN_ML, "max_ml": WATER_MAX_ML,
        })
    return [WARN_WATER_OUT_OF_RANGE]


def set_manual_targets(
    *,
    user,
    calories_kcal: int | None,
    water_ml: int | None,
    confirm_deviation: bool = False,
    confirm_water_out_of_range: bool = False,
) -> tuple[NutritionProfile, dict[str, Any]]:
    """Записать ручную норму. Возвращает профиль и отчёт о проверках.

    Отчёт: ``{"warnings": [...], "deviation": {...}|None, "set": [...]}``.
    Ничего не пишется, пока все проверки не пройдены: отказ по калориям
    не оставляет за собой записанную воду.
    """
    if calories_kcal is None and water_ml is None:
        raise NothingToSet()

    with transaction.atomic():
        profile, _ = NutritionProfile.objects.select_for_update().get_or_create(
            user=user, defaults={"activity_coefficient": DEFAULT_ACTIVITY},
        )

        warnings: list[str] = []
        deviation: dict[str, Any] | None = None
        if calories_kcal is not None:
            cal_warnings, deviation = _check_calories(
                profile, calories_kcal, confirm_deviation=confirm_deviation,
            )
            warnings.extend(cal_warnings)
        if water_ml is not None:
            warnings.extend(_check_water(water_ml, confirm_out_of_range=confirm_water_out_of_range))

        set_fields: list[str] = []
        if calories_kcal is not None:
            profile.daily_kcal = calories_kcal
            set_fields.append("daily_kcal")
        if water_ml is not None:
            profile.daily_water_ml = water_ml
            set_fields.append("daily_water_ml")
        # DRF-1929 (F1(б)): прежде здесь стоял ``elif``, гасивший
        # справочную воду в ``NULL`` при ручных калориях. Он был не
        # правилом, а КОМПЕНСАЦИЕЙ одной подписи на набор: под именем
        # ``user_entered`` справочная вода стала бы «числом человека»,
        # которого он не называл, и честнее было её стереть. Теперь у
        # воды своя подпись — стирать нечего, и человек не теряет число,
        # которого не отменял.
        for name in COMPUTED_FIELDS:
            setattr(profile, name, None)

        now = datetime.now(dt_tz.utc)
        # DRF-2192: у строки до DRF-1929 подписи по видам нет, и нетронутый
        # вид читается через общую подпись. Сменить общую на
        # ``user_entered``, не закрепив прежнюю за нетронутым видом, значило
        # бы молча объявить ручным расчёт, который человек не трогал.
        for kind, field in KIND_SOURCE_FIELD.items():
            if getattr(profile, field) is None:
                setattr(profile, field, profile.targets_source)
        # Общая подпись остаётся ради читателей до-DRF-1929 и внешнего
        # контракта; истина теперь по видам, и меняется ТОЛЬКО тот вид,
        # который человек действительно задал.
        profile.targets_source = NutritionProfile.TargetsSource.USER_ENTERED
        if calories_kcal is not None:
            profile.calories_source = NutritionProfile.TargetsSource.USER_ENTERED
            profile.calories_confirmed_at = now
        if water_ml is not None:
            profile.fluids_source = NutritionProfile.TargetsSource.USER_ENTERED
            profile.fluids_confirmed_at = now
        profile.targets_method_versions = {}
        profile.targets_input_snapshot = {}
        profile.targets_computed_at = None
        profile.targets_confirmed_at = now
        # DRF-2193, вариант (i): рядом с ручным видом предложения нет. Если
        # оно лежало (вид был расчётом, человек заменил его своим числом),
        # вид из него снимается — иначе подтверждение потом переписало бы
        # число человека расчётом, которое он только что отверг.
        profile.pending_proposal = _without_kinds(
            profile.pending_proposal,
            {
                kind
                for kind, given in (("calories", calories_kcal), ("fluids", water_ml))
                if given is not None
            },
        )
        profile.goal_overridden_by = ""
        # Аудит: что задано рукой и с какими предупреждениями — дописывается,
        # прежние записи (отказы, лестница) не стираются.
        audit = list(profile.last_overrides_applied or [])
        audit.append({
            "reason": "user_entered",
            "fields": list(set_fields),
            "warnings": list(warnings),
        })
        profile.last_overrides_applied = audit
        profile.save()

    return profile, {"warnings": warnings, "deviation": deviation, "set": set_fields}


def _without_kinds(pending: dict[str, Any] | None, kinds: set[str]) -> dict[str, Any] | None:
    """Предложение без названных видов; ``None``, если видов не осталось."""
    if not pending or not kinds:
        return pending
    remaining = [k for k in pending.get("kinds", []) if k not in kinds]
    if not remaining:
        return None
    keep = {name for kind in remaining for name in KIND_FIELDS[kind]}
    return {
        **pending,
        "kinds": remaining,
        "values": {k: v for k, v in (pending.get("values") or {}).items() if k in keep},
    }
