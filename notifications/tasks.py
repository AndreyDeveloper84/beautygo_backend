"""Celery tasks for the notifications stack.

Two task families:

- ``deliver_notification`` — fired by ``NotificationService.send`` for
  each persisted row. Idempotent: re-runs against an already-SENT row
  short-circuit.
- ``dispatch_appointment_reminders`` — beat task; every 5 min it scans
  for confirmed appointments starting ~60 min from now that don't yet
  have a reminder Notification. The 60-min anchor is a window
  ``[55min, 65min]`` to absorb beat jitter.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)


# Beat fires every 5 min. Each invocation considers appointments whose
# start_datetime falls inside [now+55min, now+65min] — that's the window
# where we send the 1h reminder. A wider window risks duplicates if beat
# has clock drift; the absent-row check below makes that safe but the
# DB cost is wasted. 10 min covers normal jitter.
REMINDER_WINDOW_MINUTES = 10
REMINDER_LEAD_MINUTES = 60
REMINDER_TEMPLATE_ID = "appointment_reminder_1h"


@shared_task(name="notifications.deliver_notification", bind=True, max_retries=3)
def deliver_notification(self, notification_id: str) -> None:
    """Send an individual notification. Retries on transient errors with
    exponential backoff. Permanent failures land on the row as
    ``status=FAILED`` and don't retry — those are message-quality bugs
    (missing token, missing phone), not transport hiccups."""
    from .services.dispatcher import NotificationService

    try:
        notification = Notification.objects.get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.warning("deliver_notification.row_missing id=%s", notification_id)
        return

    try:
        NotificationService().deliver(notification)
    except Exception as exc:  # noqa: BLE001
        logger.exception("deliver_notification.error id=%s", notification_id)
        # Retry — transport blip. exponential 30s / 60s / 120s.
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))


@shared_task(name="notifications.dispatch_appointment_reminders")
def dispatch_appointment_reminders() -> dict:
    """Find confirmed appointments ~60 min from now and queue their 1h
    reminder. Idempotent — already-reminded appointments are skipped.

    Returns a small status dict for monitoring."""
    from appointments.models import Appointment
    from .services.dispatcher import NotificationService

    now = timezone.now()
    window_start = now + timedelta(
        minutes=REMINDER_LEAD_MINUTES - REMINDER_WINDOW_MINUTES // 2,
    )
    window_end = now + timedelta(
        minutes=REMINDER_LEAD_MINUTES + REMINDER_WINDOW_MINUTES // 2,
    )

    # Confirmed-only — pending / awaiting_payment shouldn't ring the
    # client's phone yet. Reschedules update start_datetime, so an
    # appointment moved out of the window won't get its reminder twice.
    upcoming = Appointment.objects.filter(
        status=Appointment.Status.CONFIRMED,
        start_datetime__gte=window_start,
        start_datetime__lte=window_end,
    ).select_related("client", "specialist", "service")

    queued = 0
    skipped = 0
    service = NotificationService()
    for appointment in upcoming:
        if Notification.objects.filter(
            user_id=appointment.client_id,
            template_id=REMINDER_TEMPLATE_ID,
            data__appointment_id=str(appointment.id),
        ).exists():
            skipped += 1
            continue

        service.send(
            user=appointment.client,
            template_id=REMINDER_TEMPLATE_ID,
            context={
                "specialist_name": appointment.specialist.display_name,
                "service_name": appointment.service.name,
                "date_time": appointment.start_datetime.strftime("%H:%M %d.%m"),
                "address": appointment.specialist.address or "",
                "appointment_id": str(appointment.id),
            },
        )
        queued += 1

    if queued or skipped:
        logger.info(
            "reminders.dispatched queued=%d skipped=%d", queued, skipped,
        )
    return {"queued": queued, "skipped": skipped}


# ---------------------------------------------------------------------------
# Retention beat tasks — Slice N4
# ---------------------------------------------------------------------------


@shared_task(name="notifications.dispatch_water_reminders")
def dispatch_water_reminders() -> dict:
    """Push the water reminder to active clients who are behind on goal.

    Active = at least one WaterLog in the last
    ``WATER_REMINDER_ACTIVE_WINDOW_DAYS`` days. Behind = today's
    water_ml < goal × ``WATER_REMINDER_BEHIND_PCT``. Idempotent
    per-user-per-window via Notification existence check on
    (user, template_id='water_reminder', date(created_at)=today).

    Beat fires twice daily (14:00 and 18:00 UTC); the per-window
    de-dup key resets at midnight UTC, so two reminders/day max.
    """
    from datetime import datetime, timezone as dt_tz

    from django.conf import settings as dj_settings
    from django.db.models import Sum

    from nutrition.models import WaterLog
    from .services.dispatcher import NotificationService

    today = datetime.now(dt_tz.utc).date()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=dt_tz.utc)
    today_end = datetime.combine(today, datetime.max.time(), tzinfo=dt_tz.utc)
    active_since = today_start - timedelta(
        days=dj_settings.WATER_REMINDER_ACTIVE_WINDOW_DAYS,
    )
    behind_threshold = (
        dj_settings.NUTRITION_DEFAULT_WATER_GOAL_ML
        * dj_settings.WATER_REMINDER_BEHIND_PCT
    )

    # Active users: distinct user_ids with any WaterLog in the lookback
    # window. Bounded by the active-user count, not the total user table.
    active_user_ids = list(
        WaterLog.objects
        .filter(logged_at__gte=active_since)
        .values_list("user_id", flat=True).distinct()
    )

    if not active_user_ids:
        return {"queued": 0, "skipped": 0}

    # Today's totals per user — single aggregate query.
    todays_totals = dict(
        WaterLog.objects
        .filter(
            user_id__in=active_user_ids,
            logged_at__gte=today_start,
            logged_at__lte=today_end,
        )
        .values_list("user_id")
        .annotate(s=Sum("amount_ml"))
        .values_list("user_id", "s")
    )

    # Already-reminded today — pulled in one query, used as a set lookup.
    already_reminded = set(
        Notification.objects
        .filter(
            template_id="water_reminder",
            user_id__in=active_user_ids,
            created_at__gte=today_start,
        )
        .values_list("user_id", flat=True).distinct()
    )

    queued = 0
    skipped = 0
    service = NotificationService()
    # Lazy User load: select_related not needed (template only uses
    # water_ml / goal). Plain user fetch by id.
    from users.models import User

    for user in User.objects.filter(id__in=active_user_ids):
        if user.id in already_reminded:
            skipped += 1
            continue
        water_ml = int(todays_totals.get(user.id) or 0)
        if water_ml >= behind_threshold:
            skipped += 1
            continue
        service.send(
            user=user, template_id="water_reminder",
            context={
                "water_ml": water_ml,
                "water_goal_ml": dj_settings.NUTRITION_DEFAULT_WATER_GOAL_ML,
            },
        )
        queued += 1

    if queued or skipped:
        logger.info(
            "water_reminders.dispatched queued=%d skipped=%d",
            queued, skipped,
        )
    return {"queued": queued, "skipped": skipped}


@shared_task(name="notifications.dispatch_beauty_insights")
def dispatch_beauty_insights() -> dict:
    """Weekly beauty-insight push for active users.

    Caps users-per-tick at ``BEAUTY_INSIGHT_USER_CAP`` to bound the
    LLM bill. Each user gets at most one insight per Monday — the
    Notification de-dup is "any beauty_insight Notification in the
    last 6 days" rather than today-only, so a manual replay can't
    double-spend.

    The actual LLM call lives in ``ai.services.llm_client``; this
    task assembles the prompt context and persists the resulting
    text into ``Notification.data`` for the in-app feed. If the LLM
    call fails for one user, log + continue — one bad user shouldn't
    stop the rest of the cohort.
    """
    from datetime import datetime, timezone as dt_tz

    from django.conf import settings as dj_settings

    from nutrition.models import FoodLog
    from .services.dispatcher import NotificationService

    now = datetime.now(dt_tz.utc)
    cutoff = now - timedelta(days=6)

    # Active users: at least one FoodLog in the last week. Skip dormant
    # accounts so we don't burn LLM tokens on cold users.
    active_user_ids = list(
        FoodLog.objects
        .filter(logged_at__gte=now - timedelta(days=7))
        .values_list("user_id", flat=True).distinct()
    )[: dj_settings.BEAUTY_INSIGHT_USER_CAP]

    if not active_user_ids:
        return {"queued": 0, "skipped": 0}

    already_sent = set(
        Notification.objects
        .filter(
            template_id="beauty_insight",
            user_id__in=active_user_ids,
            created_at__gte=cutoff,
        )
        .values_list("user_id", flat=True).distinct()
    )

    queued = 0
    skipped = 0
    failed = 0
    service = NotificationService()
    insight_builder = _build_insight_text

    from users.models import User

    for user in User.objects.filter(id__in=active_user_ids):
        if user.id in already_sent:
            skipped += 1
            continue
        try:
            text = insight_builder(user)
        except Exception:  # noqa: BLE001 — LLM blip shouldn't break the cohort
            logger.exception(
                "beauty_insight.build_failed user_id=%s", user.id,
            )
            failed += 1
            continue
        if not text:
            skipped += 1
            continue
        service.send(
            user=user, template_id="beauty_insight",
            context={"insight_text": text},
        )
        queued += 1

    if queued or skipped or failed:
        logger.info(
            "beauty_insights.dispatched queued=%d skipped=%d failed=%d",
            queued, skipped, failed,
        )
    return {"queued": queued, "skipped": skipped, "failed": failed}


def _build_insight_text(user) -> str:
    """Generate the per-user weekly insight string.

    Stub for MVP — real LLM integration ships when the AI cost-cap
    pipeline lands (open follow-up). For pilot we ship a deterministic
    "you logged N meals" line so the beat task is end-to-end testable
    without burning tokens.
    """
    from datetime import datetime, timedelta as td, timezone as dt_tz

    from nutrition.models import FoodLog

    week_ago = datetime.now(dt_tz.utc) - td(days=7)
    count = FoodLog.objects.filter(
        user=user, logged_at__gte=week_ago,
    ).count()
    if count == 0:
        return ""
    return (
        f"За неделю вы записали {count} приёмов пищи. "
        "Так держать!"
    )
