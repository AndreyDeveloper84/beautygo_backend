"""GET/POST handlers for ``/api/v1/nutrition/internal/profile/`` (DRF-300).

Sits between the views and ``NutritionProfile``/``compute_norms``. Owns:

- PATCH-semantics upsert: only fields present in the request mutate
- ``_skipped_fields``-driven flag flips (``weight_skipped``, etc.)
- Idempotency-Key 24h replay cache via ``ProfileIdempotencyKey``
- Recompute & persist post-override norms on every write
- Lifecycle markers (``onboarded_at`` first-flip on ``complete=true``)
- Wire response builder shared with GET
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as dt_tz
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from nutrition.models import NutritionProfile, ProfileIdempotencyKey
from nutrition.services.nutrition_profile_service import (
    DEFAULT_ACTIVITY,
    ProfileInputs,
    compute_norms,
)
from nutrition.services.outbox_service import enqueue_profile_updated


IDEMPOTENCY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------


def _cache_get(key: str, user_id: int) -> dict | None:
    if not key:
        return None
    row = ProfileIdempotencyKey.objects.filter(
        key=key, user_id=user_id, expires_at__gt=datetime.now(dt_tz.utc),
    ).first()
    return row.response if row else None


def _cache_set(key: str, user_id: int, response: dict) -> None:
    if not key:
        return
    # Roundtrip through DjangoJSONEncoder so datetimes become ISO strings
    # — matches what DRF would render and keeps the JSONField column
    # JSON-safe across drivers.
    serializable = json.loads(json.dumps(response, cls=DjangoJSONEncoder))
    ProfileIdempotencyKey.objects.update_or_create(
        key=key,
        defaults={
            "user_id": user_id,
            "response": serializable,
            "expires_at": datetime.now(dt_tz.utc) + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        },
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_profile_response(user, external_user_id: str) -> dict:
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is None:
        return {"external_user_id": external_user_id, "exists": False}
    return _serialize(profile, external_user_id, exists=True)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def upsert_profile(
    *,
    user,
    external_user_id: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict:
    cached = _cache_get(idempotency_key or "", user.id)
    if cached is not None:
        return cached

    with transaction.atomic():
        profile, _ = NutritionProfile.objects.select_for_update().get_or_create(
            user=user,
            defaults={"activity_coefficient": DEFAULT_ACTIVITY},
        )
        _apply_patch(profile, payload)
        _recompute_and_persist(profile)
        _flip_lifecycle_markers(profile, payload)
        profile.save()

    response = _serialize(profile, external_user_id, exists=True)
    _cache_set(idempotency_key or "", user.id, response)
    enqueue_profile_updated(
        external_user_id=external_user_id,
        profile_payload={
            "goal": response["goal"],
            "pace": response["pace"],
            "norms": response["norms"],
            "goal_overridden_by": response["goal_overridden_by"],
            "onboarded_at": response["onboarded_at"],
        },
    )
    return response


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_DIRECT_FIELDS = (
    "gender", "age", "height_cm", "weight_kg", "weight_range",
    "activity_coefficient", "goal", "pace", "diet_preference", "timezone",
)


def _apply_patch(profile: NutritionProfile, payload: dict) -> None:
    for f in _DIRECT_FIELDS:
        if f in payload:
            setattr(profile, f, payload[f])

    flags = dict(profile.health_flags or {})
    incoming_flags = payload.get("health_flags")
    if incoming_flags:
        flags.update(incoming_flags)

    skipped = payload.get("_skipped_fields") or []
    for field in skipped:
        flags[f"{field}_skipped"] = True

    profile.health_flags = flags

    if "disclaimer_acked" in payload:
        profile.disclaimer_acked = payload["disclaimer_acked"]


def _recompute_and_persist(profile: NutritionProfile) -> None:
    norms = compute_norms(ProfileInputs(
        gender=profile.gender or "female",
        age=profile.age,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        activity_coefficient=profile.activity_coefficient or DEFAULT_ACTIVITY,
        goal=profile.goal or "maintain",
        pace=profile.pace or "moderate",
        health_flags=profile.health_flags or {},
    ))
    profile.bmr = norms.bmr
    profile.daily_kcal = norms.daily_kcal
    profile.daily_protein_g = norms.daily_protein_g
    profile.daily_fat_g = norms.daily_fat_g
    profile.daily_carbs_g = norms.daily_carbs_g
    # ``profile.daily_water_ml`` здесь БОЛЬШЕ НЕ ПИШЕТСЯ: формула
    # 30 мл × вес снята (§82, §85). Столбец остаётся в схеме со своим
    # ``default=0`` — миграция данных существующих клиентов это
    # отдельный срез, и подставлять в неё нынешние числа как «выбор
    # клиента» нельзя. Новых фиктивных значений с этой правки не
    # появляется ни у кого.
    # DRF-265: micronutrient RDA — recomputed on every upsert.
    profile.daily_vitamin_d_iu = norms.daily_vitamin_d_iu
    profile.daily_vitamin_b12_mcg = norms.daily_vitamin_b12_mcg
    profile.daily_vitamin_c_mg = norms.daily_vitamin_c_mg
    profile.daily_iron_mg = norms.daily_iron_mg
    profile.daily_calcium_mg = norms.daily_calcium_mg
    profile.daily_magnesium_mg = norms.daily_magnesium_mg
    profile.daily_omega3_g = norms.daily_omega3_g
    profile.daily_fiber_g = norms.daily_fiber_g
    profile.goal = norms.goal
    profile.pace = norms.pace
    profile.goal_overridden_by = norms.goal_overridden_by
    # DRF-1339 добавлял сюда ``{"reason": "assumed_input", "field":
    # "weight_kg"}`` — маркер того, что нормы посчитаны от
    # ``DEFAULT_WEIGHT_KG``, а не от названного человеком веса. Маркер
    # снят вместе с подстановкой: он был ПРИЗНАНИЕМ, а не отказом, и
    # число всё равно считалось, уезжало в профиль и показывалось.
    #
    # Теперь недостающий вход отменяет расчёт, и запись об этом делает
    # сам ``compute_norms``: ``{"reason": "insufficient_inputs",
    # "fields": [...]}``. Одно имя вместо двух, и оно означает «расчёта
    # нет», а не «расчёт есть, но входы чужие».
    profile.last_overrides_applied = list(norms.overrides_applied)

    # ── Происхождение (DRF-1623 N-d) ────────────────────────────────────
    #
    # Пишется на КАЖДОМ пересчёте, вместе со значением, а не отдельным
    # вызовом: разъехаться они не должны. Ориентир, у которого значение
    # новое, а происхождение старое, хуже, чем ориентир без происхождения
    # — он выглядит объяснённым.
    #
    # Отказ (нехватка входов) не выдаётся за расчёт: снимок пуст, версий
    # нет, источник — «ориентира нет». Прежнее происхождение при этом
    # СТИРАЕТСЯ намеренно: значения обнулены строкой выше, и оставить
    # рядом с нулями объяснение прошлого расчёта значило бы объяснить
    # число, которого больше нет.
    if norms.computed:
        profile.targets_source = NutritionProfile.TargetsSource.AYLA_CALCULATED
        profile.targets_method_versions = dict(norms.method_versions)
        profile.targets_input_snapshot = dict(norms.input_snapshot)
        profile.targets_computed_at = datetime.now(dt_tz.utc)
    else:
        profile.targets_source = NutritionProfile.TargetsSource.NONE
        profile.targets_method_versions = {}
        profile.targets_input_snapshot = {}
        profile.targets_computed_at = None


def _flip_lifecycle_markers(profile: NutritionProfile, payload: dict) -> None:
    if payload.get("complete") and profile.onboarded_at is None:
        profile.onboarded_at = datetime.now(dt_tz.utc)


def _serialize(
    profile: NutritionProfile, external_user_id: str, *, exists: bool,
) -> dict:
    return {
        "external_user_id": external_user_id,
        "exists": exists,
        "gender": profile.gender or None,
        "age": profile.age,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "weight_range": profile.weight_range or None,
        "activity_coefficient": profile.activity_coefficient,
        "goal": profile.goal or None,
        "pace": profile.pace or None,
        "diet_preference": profile.diet_preference or "none",
        "norms": {
            "bmr": profile.bmr,
            "daily_kcal": profile.daily_kcal,
            "daily_protein_g": profile.daily_protein_g,
            "daily_fat_g": profile.daily_fat_g,
            "daily_carbs_g": profile.daily_carbs_g,
            # ``daily_water_ml`` из ответа снят вместе с формулой,
            # которая его считала. Ключа нет — не ноль и не null: у
            # существующих строк в столбце ещё лежит старое 30 × вес, и
            # отдать его значило бы выдать снятую методику за живую.
            # DRF-265: micronutrient RDA targets.
            "daily_vitamin_d_iu": profile.daily_vitamin_d_iu,
            "daily_vitamin_b12_mcg": profile.daily_vitamin_b12_mcg,
            "daily_vitamin_c_mg": profile.daily_vitamin_c_mg,
            "daily_iron_mg": profile.daily_iron_mg,
            "daily_calcium_mg": profile.daily_calcium_mg,
            "daily_magnesium_mg": profile.daily_magnesium_mg,
            "daily_omega3_g": profile.daily_omega3_g,
            "daily_fiber_g": profile.daily_fiber_g,
        },
        "health_flags": profile.health_flags or {},
        "goal_overridden_by": profile.goal_overridden_by or None,
        "bmi_warning_overridden_at": _strip_microseconds(
            profile.bmi_warning_overridden_at,
        ),
        "overrides_applied": profile.last_overrides_applied or [],
        # DRF-1339: список входов, которые ПОДСТАВЛЯЛИСЬ вместо ответов
        # человека. Подстановки больше нет — недостающий вход отменяет
        # расчёт, — поэтому список всегда пуст, а имя отказа приезжает в
        # ``overrides_applied`` выше как ``insufficient_inputs``.
        #
        # Ключ оставлен: его читают потребители, а исчезновение
        # означало бы для них «подстановок не было», что для строк,
        # посчитанных ДО этой правки, неправда. Пустой список честнее:
        # с этой правки подстановок действительно нет ни одной.
        "assumed_inputs": [],
        # Происхождение ориентира (DRF-1623 N-d). Уезжает наружу, потому
        # что §92 п.5 адресован ПОКАЗУ: «уже рассчитанный ориентир нельзя
        # продолжать показывать как актуальный без его происхождения».
        # Значит показывающая сторона обязана его получить — иначе
        # правило неисполнимо в принципе, а не просто не исполнено.
        #
        # Снимок входов наружу НЕ уходит: он нужен для воспроизводимости
        # расчёта на нашей стороне, а не экрану. Отдать его значило бы
        # разослать параметры тела туда, где они не нужны, — ровно то,
        # чего §92 избегает раздельными согласиями.
        "targets_provenance": {
            "source": profile.targets_source,
            "method_versions": profile.targets_method_versions or {},
            "computed_at": _strip_microseconds(profile.targets_computed_at),
        },
        "disclaimer_acked": profile.disclaimer_acked,
        "onboarded_at": _strip_microseconds(profile.onboarded_at),
        "first_food_logged_at": _strip_microseconds(profile.first_food_logged_at),
        "weekly_summary_unlocked_at": _strip_microseconds(
            profile.weekly_summary_unlocked_at,
        ),
        "created_at": _strip_microseconds(profile.created_at),
        "updated_at": _strip_microseconds(profile.updated_at),
    }


def _strip_microseconds(value):
    """Render datetimes as ms-precision ISO strings.

    Both the live POST response and the idempotency-cache replay must
    render datetimes identically. DRF's default JSON encoder keeps
    millisecond precision (``.NNNZ``); DjangoJSONEncoder keeps full
    microseconds — the cache roundtrip would diverge from the live
    response without this. Returning a string from ``_serialize`` makes
    DRF pass it through verbatim, eliminating the format mismatch.
    """
    if value is None:
        return None
    ms = value.microsecond // 1000
    base = value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    suffix = f".{ms:03d}Z" if value.tzinfo else f".{ms:03d}"
    return base + suffix
