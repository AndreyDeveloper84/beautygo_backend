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
    profile.daily_water_ml = norms.daily_water_ml
    profile.goal = norms.goal
    profile.pace = norms.pace
    profile.goal_overridden_by = norms.goal_overridden_by
    profile.last_overrides_applied = norms.overrides_applied


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
            "daily_water_ml": profile.daily_water_ml,
        },
        "health_flags": profile.health_flags or {},
        "goal_overridden_by": profile.goal_overridden_by or None,
        "bmi_warning_overridden_at": _strip_microseconds(
            profile.bmi_warning_overridden_at,
        ),
        "overrides_applied": profile.last_overrides_applied or [],
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
