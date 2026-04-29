"""Notification template registry.

Each template names exactly one event-to-message mapping. Adding a new
template means adding an entry here — never composing strings inside a
view or service. This keeps copy / tone reviewable in one place and
gives ops a stable ``template_id`` to filter on.

Body / title / sms_text are rendered via ``str.format(**context)`` so
every placeholder must exist in the context dict at dispatch time.
Missing keys raise ``KeyError`` — surface that in tests rather than
shipping a partially-formatted message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Notification


# Deep-link prefix per app. Mobile apps register these schemes — see the
# Two Apps section in CLAUDE.md.
DEEP_LINKS: dict[str, str] = {
    "client": "beautygo-client://",
    "pro": "beautygo-pro://",
}


AppType = Literal["client", "pro"]


@dataclass(frozen=True)
class NotificationTemplate:
    id: str
    app_type: AppType                      # which mobile app gets it
    channel: Notification.Channel
    push_title: str
    push_body: str
    sms_text: str = ""                     # required when channel != PUSH
    deep_link: str = ""                    # relative path, prefixed at render

    def render(self, context: dict) -> dict:
        """Render the template against ``context`` and return a dict
        with rendered ``title``, ``body``, ``sms_text``, ``deep_link``.

        Raises ``KeyError`` on a missing context key — a missing field is
        a programmer error, not a user-facing one. Tests catch it before
        the message ever reaches Celery.
        """
        prefix = DEEP_LINKS.get(self.app_type, "")
        deep_link = (prefix + self.deep_link.format(**context)) if self.deep_link else ""
        return {
            "title": self.push_title.format(**context),
            "body": self.push_body.format(**context),
            "sms_text": self.sms_text.format(**context) if self.sms_text else "",
            "deep_link": deep_link,
        }


TEMPLATES: dict[str, NotificationTemplate] = {
    "appointment_created_client": NotificationTemplate(
        id="appointment_created_client",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Запись подтверждена",
        push_body="{service_name} у {specialist_name}, {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_created_specialist": NotificationTemplate(
        id="appointment_created_specialist",
        app_type="pro",
        channel=Notification.Channel.PUSH,
        push_title="Новая запись",
        push_body="{client_name} на {service_name}, {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_reminder_1h": NotificationTemplate(
        id="appointment_reminder_1h",
        app_type="client",
        # Reminders are critical — fall back to SMS if push doesn't deliver.
        # The dispatcher only fires the SMS when the push fails or the user
        # has no active device token.
        channel=Notification.Channel.BOTH,
        push_title="Напоминание о записи",
        push_body="Через 1 час у вас запись к {specialist_name}",
        sms_text="BeautyGO: через 1 час запись к {specialist_name} ({date_time}). {address}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_cancelled": NotificationTemplate(
        id="appointment_cancelled",
        # Sent to both sides — caller picks app_type per recipient.
        # Keeping a single template means the body stays consistent; the
        # dispatcher resolves which client gets it.
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Запись отменена",
        push_body="Запись на {date_time} отменена",
        deep_link="appointment/{appointment_id}",
    ),
    "payment_paid": NotificationTemplate(
        id="payment_paid",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Оплата прошла",
        push_body="Оплата {amount} ₽ за {service_name} прошла успешно",
        deep_link="appointment/{appointment_id}",
    ),
    # --- Slice N4 additions -----------------------------------------------
    # New strings use the Ayla brand (per CLAUDE.md Brand Migration Status).
    # The five templates above predate the rebrand and are migrated as a
    # separate ticket so this slice stays focused on event coverage.
    "appointment_confirmed_client": NotificationTemplate(
        id="appointment_confirmed_client",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Запись подтверждена",
        push_body="{specialist_name} подтвердил(а) запись на {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_cancelled_specialist": NotificationTemplate(
        id="appointment_cancelled_specialist",
        app_type="pro",
        channel=Notification.Channel.PUSH,
        push_title="Запись отменена",
        push_body="{client_name} отменил(а) запись на {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_rescheduled_client": NotificationTemplate(
        id="appointment_rescheduled_client",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Запись перенесена",
        push_body="С {old_date_time} на {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "appointment_rescheduled_specialist": NotificationTemplate(
        id="appointment_rescheduled_specialist",
        app_type="pro",
        channel=Notification.Channel.PUSH,
        push_title="Запись перенесена",
        push_body="{client_name}: с {old_date_time} на {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "booking_no_show_specialist": NotificationTemplate(
        id="booking_no_show_specialist",
        app_type="pro",
        channel=Notification.Channel.PUSH,
        push_title="Клиент не пришёл",
        push_body="{client_name} не пришёл(ла) на запись {date_time}",
        deep_link="appointment/{appointment_id}",
    ),
    "review_request": NotificationTemplate(
        id="review_request",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Оцените мастера",
        push_body=(
            "Поделитесь впечатлением от {service_name} у {specialist_name}"
        ),
        deep_link="appointment/{appointment_id}/review",
    ),
    "payment_refunded": NotificationTemplate(
        id="payment_refunded",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Возврат оформлен",
        # ``is_partial`` is bool in the context; Python format spec
        # renders True/False — fine for MVP, mobile shows the body
        # verbatim. Two templates would be cleaner but doubles the
        # surface for tone reviews.
        push_body="Возврат {amount} ₽ за {service_name} оформлен",
        deep_link="appointment/{appointment_id}",
    ),
    "payment_failed": NotificationTemplate(
        id="payment_failed",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Оплата не прошла",
        push_body="Оплата за {service_name} не прошла. Попробуйте ещё раз",
        deep_link="appointment/{appointment_id}",
    ),
    # Retention reminders (M5 retention; beat tasks defined in
    # notifications/tasks.py). Context: water_ml, water_goal_ml.
    "water_reminder": NotificationTemplate(
        id="water_reminder",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Время выпить воды",
        push_body=(
            "Сегодня выпито {water_ml} мл из {water_goal_ml}. Не забывай!"
        ),
        deep_link="day",
    ),
    # Context: insight_text (LLM-generated short summary).
    "beauty_insight": NotificationTemplate(
        id="beauty_insight",
        app_type="client",
        channel=Notification.Channel.PUSH,
        push_title="Совет недели от Ayla",
        push_body="{insight_text}",
        deep_link="profile/insights",
    ),
    # `booking_suggestion` template is intentionally absent until
    # UserPersonalContext expands (DRF-174 follow-up). The trigger
    # heuristic without context is too noisy.
}


def get_template(template_id: str) -> NotificationTemplate:
    """Fetch by id — raises ``KeyError`` if unknown so a typo in the
    caller surfaces as a hard error rather than a silent skip."""
    return TEMPLATES[template_id]
