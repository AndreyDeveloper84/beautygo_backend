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
