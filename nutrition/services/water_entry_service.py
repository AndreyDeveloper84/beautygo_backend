"""WaterEntry write/read pipeline (DRF-302).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §2.

Lives next to ``WaterService`` (Slice 4 mobile glasses) but is independent —
WaterEntry is the bot-driven path with beverage support, macros, soft-delete,
and milestone detection. The two flows will reconcile in a Phase 3 cleanup
pass once mobile picks up the same model.

Profile-aware fields (timezone, daily_water_ml, pregnant, eating_disorder)
come from ``_load_nutrition_context``. Until DRF-300 ships ``NutritionProfile``,
the loader returns a defaults shim using ``settings.NUTRITION_DEFAULT_WATER_GOAL_ML``,
UTC, and pregnant=eating_disorder=False so behaviour stays predictable.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from nutrition.models import Beverage, FoodLog, WaterEntry
from nutrition.services.outbox_service import (
    enqueue_milestone_reached,
    enqueue_water_logged,
)


CAFFEINE_PREGNANT_THRESHOLD_MG = 200
RESTORE_WINDOW_MINUTES = 15
MILESTONE_THRESHOLDS = (50, 100, 150)
MAX_ML_PER_ENTRY = 3000
MIN_ML_PER_ENTRY = 10
DEFAULT_BEVERAGE_LABEL = "стакан"


# ---------------------------------------------------------------------------
# Profile context (DRF-300 will replace this loader)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NutritionContext:
    """Profile-derived inputs needed by WaterEntryService.

    Defaults are returned when no profile exists yet (pre-DRF-300 era).
    The endpoint consumer doesn't see this struct — it's an internal
    contract between the loader and the service.
    """
    timezone: dt_tz = dt_tz.utc
    daily_water_ml: int = 2000
    pregnant: bool = False
    eating_disorder: bool = False


def _load_nutrition_context(user_id: int) -> NutritionContext:
    """Read NutritionProfile (DRF-300) when present, fall back to defaults.

    The profile is optional — pre-onboarding users still log water and we
    want sensible behaviour (no eating-disorder strip, UTC, settings
    default norm). Once DRF-300 lands the profile, all four fields here
    pick up the real values and the rest of the service Just Works.
    """
    from nutrition.models import NutritionProfile  # local import to avoid cycle

    default_norm = int(getattr(settings, "NUTRITION_DEFAULT_WATER_GOAL_ML", 2000))

    try:
        profile = NutritionProfile.objects.only(
            "timezone", "daily_water_ml", "health_flags",
        ).get(user_id=user_id)
    except NutritionProfile.DoesNotExist:
        return NutritionContext(daily_water_ml=default_norm)

    tz = dt_tz.utc
    if profile.timezone and profile.timezone != "UTC":
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(profile.timezone)
        except Exception:
            tz = dt_tz.utc

    flags = profile.health_flags or {}
    return NutritionContext(
        timezone=tz,
        daily_water_ml=int(profile.daily_water_ml or default_norm),
        pregnant=bool(flags.get("pregnant")),
        eating_disorder=bool(flags.get("eating_disorder")),
    )


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class CreateWaterInput:
    user_id: int
    ml: int
    beverage_slug: str | None
    ts: datetime | None
    idempotency_key: str | None


@dataclass
class WaterEntryResponse:
    entry_id: UUID
    ml: int
    water_ml: int
    beverage_name: str | None
    beverage_label: str | None
    kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    caffeine_mg: float
    today_total_water_ml: int
    today_norm_water_ml: int
    today_progress_pct: int
    milestone_text: str | None
    alcohol_recovery_hint: bool
    caffeine_warning: str | None
    ts: datetime


@dataclass
class TodayWaterResponse:
    date: date
    entries: list[dict] = field(default_factory=list)
    today_total_water_ml: int = 0
    today_norm_water_ml: int = 0
    today_kcal_from_beverages: float = 0.0
    today_caffeine_mg: float = 0.0
    today_total_coffee_cups: int = 0
    today_total_tea_cups: int = 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidMlError(ValueError):
    pass


class UnknownBeverageError(ValueError):
    pass


class EntryNotFoundError(LookupError):
    pass


class RestoreWindowExpiredError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WaterEntryService:
    """Single entry point for POST/DELETE/restore/today flows."""

    # ------------------------------------------------------------------
    # POST /water/
    # ------------------------------------------------------------------

    def create(self, payload: CreateWaterInput) -> WaterEntryResponse:
        if payload.ml < MIN_ML_PER_ENTRY or payload.ml > MAX_ML_PER_ENTRY:
            raise InvalidMlError(
                f"ml must be in [{MIN_ML_PER_ENTRY}, {MAX_ML_PER_ENTRY}]"
            )

        # Idempotency replay — return the existing entry's response so the
        # bot's retry loop is safe regardless of how the upstream call timed
        # out. Soft-deleted entries don't qualify (treat them as gone).
        if payload.idempotency_key:
            existing = WaterEntry.objects.filter(
                idempotency_key=payload.idempotency_key,
                deleted_at__isnull=True,
            ).first()
            if existing is not None:
                return self._build_response(existing)

        beverage = self._resolve_beverage(payload.beverage_slug)
        ts = payload.ts or datetime.now(dt_tz.utc)
        ctx = _load_nutrition_context(payload.user_id)

        macros = self._compute_macros(payload.ml, beverage)

        # today_total computed BEFORE insert so milestone detection sees the
        # pre-state. The current entry's contribution is added in-memory to
        # decide which threshold (if any) it crossed.
        before_total = self._sum_water_ml(payload.user_id, ts.date(), ctx.timezone)
        new_total = before_total + macros["water_ml"]
        milestone = self._milestone_crossed(
            payload.user_id,
            ctx,
            ts.date(),
            before=before_total,
            after=new_total,
        )

        with transaction.atomic():
            food_log = None
            if macros["kcal"] > 0:
                food_log = self._create_food_log_mirror(
                    user_id=payload.user_id,
                    beverage=beverage,
                    ts=ts,
                    macros=macros,
                )

            entry = WaterEntry.objects.create(
                user_id=payload.user_id,
                beverage=beverage,
                ts=ts,
                ml=payload.ml,
                water_ml=macros["water_ml"],
                kcal=macros["kcal"],
                protein_g=macros["protein_g"],
                fat_g=macros["fat_g"],
                carbs_g=macros["carbs_g"],
                sugar_g=macros["sugar_g"],
                caffeine_mg=macros["caffeine_mg"],
                food_log=food_log,
                milestone_threshold=milestone,
                idempotency_key=payload.idempotency_key,
            )
            external_user_id = self._external_user_id_for(payload.user_id)
            if external_user_id:
                enqueue_water_logged(
                    external_user_id=external_user_id,
                    entry_payload={
                        "entry_id": str(entry.id),
                        "ml": entry.ml,
                        "water_ml": int(entry.water_ml),
                        "beverage_slug": beverage.slug if beverage else None,
                        "ts": entry.ts.isoformat(),
                    },
                )
                if milestone is not None:
                    enqueue_milestone_reached(
                        external_user_id=external_user_id,
                        threshold=milestone,
                        day=entry.ts.date().isoformat(),
                        today_total_water_ml=int(new_total),
                    )

        return self._build_response(entry)

    def _external_user_id_for(self, user_id: int) -> str | None:
        """Look up external_user_id (e.g. ``bot:NNN``) for outbox payloads.

        Tied to the ProxyUser pattern (DRF-246) — service-to-service
        callers create proxy users whose ``username`` is the external id.
        Returns None for non-proxy users (mobile path) so client-app
        traffic doesn't fan out to the bot.
        """
        try:
            from users.models import User
            user = User.objects.only("username", "is_proxy").get(id=user_id)
        except Exception:  # noqa: BLE001
            return None
        if not getattr(user, "is_proxy", False):
            return None
        return user.username or None

    # ------------------------------------------------------------------
    # DELETE /water/{id}/
    # ------------------------------------------------------------------

    def soft_delete(self, user_id: int, entry_id: UUID) -> WaterEntry:
        try:
            entry = WaterEntry.objects.get(
                id=entry_id, user_id=user_id, deleted_at__isnull=True,
            )
        except WaterEntry.DoesNotExist as e:
            raise EntryNotFoundError(str(entry_id)) from e

        with transaction.atomic():
            if entry.food_log_id:
                FoodLog.objects.filter(id=entry.food_log_id).delete()
                entry.food_log = None
            entry.deleted_at = datetime.now(dt_tz.utc)
            entry.deleted_reason = WaterEntry.DeletedReason.USER_UNDO
            entry.save(update_fields=["deleted_at", "deleted_reason", "food_log"])
        return entry

    # ------------------------------------------------------------------
    # POST /water/{id}/restore/
    # ------------------------------------------------------------------

    def restore(self, user_id: int, entry_id: UUID) -> WaterEntry:
        try:
            entry = WaterEntry.objects.get(
                id=entry_id, user_id=user_id, deleted_at__isnull=False,
            )
        except WaterEntry.DoesNotExist as e:
            raise EntryNotFoundError(str(entry_id)) from e

        if datetime.now(dt_tz.utc) - entry.deleted_at > timedelta(
            minutes=RESTORE_WINDOW_MINUTES,
        ):
            raise RestoreWindowExpiredError(str(entry_id))

        with transaction.atomic():
            food_log = None
            if entry.kcal > 0:
                food_log = self._create_food_log_mirror(
                    user_id=user_id,
                    beverage=entry.beverage,
                    ts=entry.ts,
                    macros={
                        "kcal": entry.kcal,
                        "protein_g": entry.protein_g,
                        "fat_g": entry.fat_g,
                        "carbs_g": entry.carbs_g,
                    },
                    idempotency_suffix=f"restore:{entry.id}",
                )
            entry.food_log = food_log
            entry.deleted_at = None
            entry.deleted_reason = ""
            entry.save(update_fields=["deleted_at", "deleted_reason", "food_log"])
        return entry

    # ------------------------------------------------------------------
    # GET /water/today/
    # ------------------------------------------------------------------

    def today(self, user_id: int) -> TodayWaterResponse:
        ctx = _load_nutrition_context(user_id)
        today = datetime.now(ctx.timezone).date()
        entries = list(
            WaterEntry.objects
            .select_related("beverage")
            .filter(
                user_id=user_id,
                ts__gte=_day_start(today, ctx.timezone),
                ts__lte=_day_end(today, ctx.timezone),
                deleted_at__isnull=True,
            )
            .order_by("ts")
        )

        total_water_ml = max(0, int(sum(e.water_ml for e in entries)))
        total_kcal = sum(e.kcal for e in entries)
        total_caffeine = sum(e.caffeine_mg for e in entries)

        coffee_cups = sum(
            1 for e in entries
            if e.beverage_id and e.beverage.category == Beverage.Category.COFFEE
        )
        tea_cups = sum(
            1 for e in entries
            if e.beverage_id and e.beverage.category == Beverage.Category.TEA
        )

        return TodayWaterResponse(
            date=today,
            entries=[
                {
                    "entry_id": e.id,
                    "ts": e.ts,
                    "ml": e.ml,
                    "water_ml": int(e.water_ml),
                    "beverage_slug": e.beverage.slug if e.beverage_id else None,
                    "beverage_name": e.beverage.name_ru if e.beverage_id else None,
                    "deleted": False,
                }
                for e in entries
            ],
            today_total_water_ml=total_water_ml,
            today_norm_water_ml=ctx.daily_water_ml,
            today_kcal_from_beverages=round(total_kcal, 1),
            today_caffeine_mg=round(total_caffeine, 1),
            today_total_coffee_cups=coffee_cups,
            today_total_tea_cups=tea_cups,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_beverage(self, slug: str | None) -> Beverage | None:
        if not slug:
            return None
        try:
            return Beverage.objects.get(slug=slug, is_active=True)
        except Beverage.DoesNotExist as e:
            raise UnknownBeverageError(slug) from e

    def _compute_macros(self, ml: int, beverage: Beverage | None) -> dict:
        if beverage is None:
            return {
                "water_ml": float(ml),
                "kcal": 0.0,
                "protein_g": 0.0,
                "fat_g": 0.0,
                "carbs_g": 0.0,
                "sugar_g": 0.0,
                "caffeine_mg": 0.0,
            }
        ratio = ml / 100.0
        return {
            "water_ml": ml * beverage.water_coefficient,
            "kcal": beverage.kcal_per_100ml * ratio,
            "protein_g": beverage.protein_g_per_100ml * ratio,
            "fat_g": beverage.fat_g_per_100ml * ratio,
            "carbs_g": beverage.carbs_g_per_100ml * ratio,
            "sugar_g": beverage.sugar_g_per_100ml * ratio,
            "caffeine_mg": beverage.caffeine_mg_per_100ml * ratio,
        }

    def _sum_water_ml(self, user_id: int, day: date, tz: dt_tz) -> float:
        total = (
            WaterEntry.objects
            .filter(
                user_id=user_id,
                ts__gte=_day_start(day, tz),
                ts__lte=_day_end(day, tz),
                deleted_at__isnull=True,
            )
            .aggregate(s=Sum("water_ml"))["s"]
        )
        return float(total or 0.0)

    def _milestone_crossed(
        self,
        user_id: int,
        ctx: NutritionContext,
        day: date,
        *,
        before: float,
        after: float,
    ) -> int | None:
        if ctx.eating_disorder:
            return None
        norm = ctx.daily_water_ml
        if norm <= 0:
            return None
        # Already-fired thresholds today — including soft-deleted entries.
        # Once the user saw "100% 🎉" we shouldn't flash it again even if
        # they undo and re-log, hence not filtering on deleted_at.
        already = set(
            WaterEntry.objects
            .filter(
                user_id=user_id,
                ts__gte=_day_start(day, ctx.timezone),
                ts__lte=_day_end(day, ctx.timezone),
                milestone_threshold__isnull=False,
            )
            .values_list("milestone_threshold", flat=True)
        )
        for threshold in MILESTONE_THRESHOLDS:
            target_ml = norm * threshold / 100
            if before < target_ml <= after and threshold not in already:
                return threshold
        return None

    def _milestone_text(self, threshold: int | None) -> str | None:
        if threshold is None:
            return None
        return {
            50: "Половина дня — отличный темп!",
            100: "Дневная норма выполнена 💧",
            150: "Сегодня воды с запасом — следи за самочувствием.",
        }[threshold]

    def _create_food_log_mirror(
        self,
        *,
        user_id: int,
        beverage: Beverage | None,
        ts: datetime,
        macros: dict,
        idempotency_suffix: str = "create",
    ) -> FoodLog:
        slug = beverage.slug if beverage is not None else "voda"
        name = beverage.name_ru if beverage is not None else "Вода"
        idem = f"water:{user_id}:{ts.isoformat()}:{slug}:{idempotency_suffix}"
        log, _ = FoodLog.objects.update_or_create(
            idempotency_key=idem,
            defaults={
                "user_id": user_id,
                "dish_name": name,
                "portion_multiplier": 1.0,
                "calories": macros.get("kcal", 0.0),
                "protein_g": macros.get("protein_g", 0.0),
                "fat_g": macros.get("fat_g", 0.0),
                "carbs_g": macros.get("carbs_g", 0.0),
                "meal_type": FoodLog.MealType.SNACK,
                "logged_at": ts,
            },
        )
        return log

    def _build_response(self, entry: WaterEntry) -> WaterEntryResponse:
        ctx = _load_nutrition_context(entry.user_id)
        total = self._sum_water_ml(entry.user_id, entry.ts.date(), ctx.timezone)
        total = max(0, int(total))
        norm = ctx.daily_water_ml
        pct = min(100, int(round(total * 100 / norm))) if norm > 0 else 0

        beverage = entry.beverage
        is_alcohol = (
            beverage is not None
            and beverage.category == Beverage.Category.ALCOHOL
        )

        # Eating disorder mode strips numeric/triumphant fields. The bot
        # still gets the entry persisted (logging is therapy-neutral); only
        # the UI cues that anchor on numbers are removed.
        if ctx.eating_disorder:
            return WaterEntryResponse(
                entry_id=entry.id,
                ml=entry.ml,
                water_ml=int(entry.water_ml),
                beverage_name=beverage.name_ru if beverage else None,
                beverage_label=beverage.default_serving_label if beverage else None,
                kcal=0.0,
                protein_g=0.0,
                fat_g=0.0,
                carbs_g=0.0,
                caffeine_mg=0.0,
                today_total_water_ml=total,
                today_norm_water_ml=norm,
                today_progress_pct=pct,
                milestone_text=None,
                alcohol_recovery_hint=False,
                caffeine_warning=None,
                ts=entry.ts,
            )

        caffeine_warning = None
        if ctx.pregnant:
            caffeine_today = self._sum_caffeine_mg(
                entry.user_id, entry.ts.date(), ctx.timezone,
            )
            if caffeine_today >= CAFFEINE_PREGNANT_THRESHOLD_MG:
                caffeine_warning = (
                    "Сегодня кофеина уже многовато — для беременности "
                    "рекомендуется до 200 мг."
                )

        return WaterEntryResponse(
            entry_id=entry.id,
            ml=entry.ml,
            water_ml=int(entry.water_ml),
            beverage_name=beverage.name_ru if beverage else None,
            beverage_label=beverage.default_serving_label if beverage else None,
            kcal=round(entry.kcal, 1),
            protein_g=round(entry.protein_g, 1),
            fat_g=round(entry.fat_g, 1),
            carbs_g=round(entry.carbs_g, 1),
            caffeine_mg=round(entry.caffeine_mg, 1),
            today_total_water_ml=total,
            today_norm_water_ml=norm,
            today_progress_pct=pct,
            milestone_text=self._milestone_text(entry.milestone_threshold),
            alcohol_recovery_hint=is_alcohol,
            caffeine_warning=caffeine_warning,
            ts=entry.ts,
        )

    def _sum_caffeine_mg(self, user_id: int, day: date, tz: dt_tz) -> float:
        total = (
            WaterEntry.objects
            .filter(
                user_id=user_id,
                ts__gte=_day_start(day, tz),
                ts__lte=_day_end(day, tz),
                deleted_at__isnull=True,
            )
            .aggregate(s=Sum("caffeine_mg"))["s"]
        )
        return float(total or 0.0)


# ---------------------------------------------------------------------------
# Idempotency helper
# ---------------------------------------------------------------------------


def make_idempotency_key(
    *, user_id: int, ts: datetime, ml: int, beverage_slug: str | None,
) -> str:
    payload = f"{user_id}|{ts.isoformat()}|{ml}|{beverage_slug or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, payload))


# ---------------------------------------------------------------------------
# 90-day purge for soft-deleted rows (DRF-302 acceptance)
# ---------------------------------------------------------------------------


def purge_deleted_water_entries(older_than_days: int = 90) -> int:
    cutoff = datetime.now(dt_tz.utc) - timedelta(days=older_than_days)
    qs = WaterEntry.objects.filter(deleted_at__lt=cutoff)
    count = qs.count()
    qs.delete()
    return count


# ---------------------------------------------------------------------------
# Day-bound helpers (TZ-aware)
# ---------------------------------------------------------------------------


def _day_start(day: date, tz: dt_tz) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _day_end(day: date, tz: dt_tz) -> datetime:
    return datetime.combine(day, time.max, tzinfo=tz)
