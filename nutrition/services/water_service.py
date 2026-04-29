"""Water tracker aggregation.

Two responsibilities:
1. Build the ``WaterLogResponse`` after POST/DELETE of a glass — sums
   today's water for the user and computes goal % so the mobile UI
   can update its progress ring without re-querying.
2. Answer GET /water/today with logs[] + aggregate.

Day boundary semantics match NutritionSummaryService: UTC calendar
day for MVP. When the user-tz field lands on UserPersonalContext,
both services migrate together.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from uuid import UUID

from django.conf import settings
from django.db.models import Sum

from nutrition.models import WaterLog


@dataclass(frozen=True)
class WaterAggregate:
    water_ml: int
    water_goal_ml: int
    water_pct: int                 # 0..100, capped


@dataclass(frozen=True)
class WaterLogCreatedResponse:
    """POST/DELETE response shape — aggregate plus the relevant log id."""
    aggregate: WaterAggregate
    log_id: UUID


@dataclass(frozen=True)
class WaterTodayResponse:
    """GET /water/today response — full list + aggregate."""
    logs: list[WaterLog]
    aggregate: WaterAggregate


class WaterService:
    """Aggregates and queries WaterLog rows for one user."""

    def aggregate_for_today(self, user_id: int) -> WaterAggregate:
        return self._aggregate_for_day(user_id, _today_utc())

    def aggregate_for_day(self, user_id: int, day: date) -> WaterAggregate:
        return self._aggregate_for_day(user_id, day)

    def today_logs(self, user_id: int) -> WaterTodayResponse:
        day = _today_utc()
        start, end = _utc_day_bounds(day)
        logs = list(
            WaterLog.objects
            .filter(user_id=user_id, logged_at__gte=start, logged_at__lte=end)
            .order_by("logged_at")
        )
        agg = self._aggregate_from_logs(logs)
        return WaterTodayResponse(logs=logs, aggregate=agg)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _aggregate_for_day(self, user_id: int, day: date) -> WaterAggregate:
        start, end = _utc_day_bounds(day)
        total = (
            WaterLog.objects
            .filter(user_id=user_id, logged_at__gte=start, logged_at__lte=end)
            .aggregate(s=Sum("amount_ml"))["s"]
        ) or 0
        goal = settings.NUTRITION_DEFAULT_WATER_GOAL_ML
        return WaterAggregate(
            water_ml=int(total),
            water_goal_ml=int(goal),
            water_pct=_pct(int(total), int(goal)),
        )

    def _aggregate_from_logs(self, logs: list[WaterLog]) -> WaterAggregate:
        total = sum(log.amount_ml for log in logs)
        goal = settings.NUTRITION_DEFAULT_WATER_GOAL_ML
        return WaterAggregate(
            water_ml=int(total),
            water_goal_ml=int(goal),
            water_pct=_pct(int(total), int(goal)),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, tzinfo=timezone.utc),
        datetime.combine(day, time.max, tzinfo=timezone.utc),
    )


def _pct(value: int, goal: int) -> int:
    if goal <= 0:
        return 0
    # Cap at 100 — UI progress ring shouldn't keep growing past goal.
    return min(100, round(value * 100 / goal))
