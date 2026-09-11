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

from django.db.models import Sum

from nutrition.models import WaterLog


@dataclass(frozen=True)
class WaterAggregate:
    """Факт и — пока не будет методики — ОТСУТСТВИЕ ориентира.

    ``water_goal_ml`` и ``water_pct`` объявлены ``None`` и другими не
    бывают: единственный источник ориентира, ``NutritionProfile.
    daily_water_ml``, считался снятой формулой 30 мл × вес (§82, §85).
    Поля оставлены на месте — они несут ФОРМУ контракта, и сериализатор
    выкидывает их из ответа именно по ``None``. Значение сюда положить
    неоткуда, и положить его — значит вернуть дефект.
    """

    water_ml: int
    water_goal_ml: int | None = None
    water_pct: int | None = None


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
        agg = self._aggregate_from_logs(logs, user_id=user_id)
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
        # Ориентира нет ни у кого — ни ``water_goal_ml``, ни ``water_pct``.
        return WaterAggregate(water_ml=int(total))

    def _aggregate_from_logs(
        self, logs: list[WaterLog], *, user_id: int
    ) -> WaterAggregate:
        # ``user_id`` пришёл в подпись вместе с нормой: раньше она была
        # общей на всех и человека не спрашивала. Подпись сохранена — она
        # ждёт методику (§85, раздел 4), после которой ориентир снова
        # станет ЧЬИМ-ТО, а не общим.
        total = sum(log.amount_ml for log in logs)
        return WaterAggregate(water_ml=int(total))


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

# ``_pct`` снят вместе с ориентиром. Процент — ПРОИЗВОДНАЯ ориентира, и
# без него он не «ноль процентов», а отсутствие ответа: доли от
# несуществующей нормы не бывает (§85, раздел 8 — «активного ориентира
# нет → шкалы нет, процента нет»).
