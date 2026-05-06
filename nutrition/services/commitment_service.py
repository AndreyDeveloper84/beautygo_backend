"""Commitment days — distinct calendar days the user has logged food.

Track E retention play (DRF-266): the 14d / 28d progressive summary
unlocks when ``commitment_days >= period``. The number is also a useful
input to the cross-domain rule engine — only mature users see
recommendations. Cap at 60 days to keep the query bounded.

Pure DB aggregation. Not user-tz aware (uses UTC) — matches the rest
of nutrition aggregation. When ``UserPersonalContext.timezone`` lands,
this becomes the natural place to switch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.db.models.functions import TruncDate

from nutrition.models import FoodLog


COMMITMENT_LOOKBACK_DAYS = 60


def get_commitment_days(user) -> int:
    """Return the count of distinct UTC days with ≥1 FoodLog in last 60 days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=COMMITMENT_LOOKBACK_DAYS)

    distinct = (
        FoodLog.objects
        .filter(user=user, logged_at__gte=start, logged_at__lte=end)
        .annotate(day=TruncDate("logged_at", tzinfo=timezone.utc))
        .values("day")
        .distinct()
        .count()
    )
    return min(distinct, COMMITMENT_LOOKBACK_DAYS)
