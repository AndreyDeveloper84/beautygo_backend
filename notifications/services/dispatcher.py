"""High-level entry point for "notify this user with this template".

Every site that wants to send a push or SMS calls
``NotificationService().send(user, template_id, context)``. The
dispatcher renders the template, persists a ``Notification`` row in
``Status.PENDING``, and queues a Celery task to actually deliver. The
delivery task updates the row to ``SENT`` or ``FAILED``.

Splitting persistence (sync, transactional) from delivery (async,
best-effort) is what makes the SLA observable: ops can ask "what was
queued in the last hour?" by looking at rows; "what's stuck?" by
filtering ``status=PENDING and created_at < now-2min``.
"""
from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from .. import templates
from ..models import Notification

logger = logging.getLogger(__name__)
User = get_user_model()


class NotificationService:
    """Singleton-style facade. Stateless — safe to instantiate per call."""

    def send(
        self,
        user,
        template_id: str,
        context: dict[str, Any],
    ) -> Notification:
        """Render template, persist a Notification row, queue delivery.

        Returns the persisted Notification (status=PENDING). Raises
        ``KeyError`` for an unknown template_id — that's a programmer
        bug, not a runtime condition.
        """
        template = templates.get_template(template_id)
        rendered = template.render(context)

        notification = Notification.objects.create(
            user=user,
            template_id=template_id,
            channel=template.channel,
            title=rendered["title"],
            body=rendered["body"],
            data=context,
            deep_link=rendered["deep_link"],
            status=Notification.Status.PENDING,
        )

        # Queue delivery. Importing here avoids a circular dependency
        # with tasks.py (which imports the dispatcher to wire outbox).
        from ..tasks import deliver_notification

        deliver_notification.delay(str(notification.id))
        return notification

    def deliver(self, notification: Notification) -> None:
        """Synchronous delivery used by the Celery task. Updates the
        row's status / sent_at / error."""
        from ..models import Notification as N

        # Re-read inside the task to avoid acting on stale data when the
        # row was updated between persist and worker pickup.
        notification.refresh_from_db()
        if notification.status != N.Status.PENDING:
            logger.info(
                "notification.skip already_status=%s id=%s",
                notification.status, notification.id,
            )
            return

        ok = self._dispatch_by_channel(notification)
        notification.status = N.Status.SENT if ok else N.Status.FAILED
        notification.sent_at = timezone.now() if ok else None
        notification.save(update_fields=["status", "sent_at", "error"])

    def _dispatch_by_channel(self, notification: Notification) -> bool:
        """Pick the right transport. Returns True iff *something* was
        delivered. For BOTH, push success short-circuits — we only fall
        back to SMS when push fails or the user has no active token."""
        from ..services.push import PushService
        from users.sms import SMSService

        channel = notification.channel
        Channel = Notification.Channel

        if channel in (Channel.PUSH, Channel.BOTH):
            tokens = self._active_tokens_for(notification)
            push_ok = False
            if tokens:
                push_ok = self._send_push_to_tokens(
                    tokens, notification, PushService(),
                )
            else:
                logger.info(
                    "notification.no_tokens user_id=%s template=%s",
                    notification.user_id, notification.template_id,
                )
            if push_ok:
                return True
            if channel == Channel.PUSH:
                notification.error = "push delivery failed (no tokens or send error)"
                return False
            # Channel.BOTH → fall through to SMS

        # SMS path (Channel.SMS or BOTH-fallback)
        phone = getattr(notification.user, "phone", "") or ""
        if not phone:
            notification.error = "user has no phone for SMS fallback"
            return False
        rendered = templates.get_template(notification.template_id).render(
            notification.data,
        )
        text = rendered["sms_text"]
        if not text:
            notification.error = "template missing sms_text for SMS channel"
            return False
        return SMSService().send(phone, text)

    def _active_tokens_for(self, notification: Notification) -> list[str]:
        """Return the list of FCM tokens to push to — filtered by the
        template's target app_type."""
        template = templates.get_template(notification.template_id)
        return list(
            notification.user.device_tokens
            .filter(app_type=template.app_type, is_active=True)
            .values_list("token", flat=True)
        )

    def _send_push_to_tokens(
        self,
        tokens: list[str],
        notification: Notification,
        push: Any,
    ) -> bool:
        """Send the same push to every token. Success if at least one
        delivers — covers the "user has tokens on two devices" case."""
        any_ok = False
        for token in tokens:
            ok = push.send(
                token=token,
                title=notification.title,
                body=notification.body,
                data={
                    "deep_link": notification.deep_link,
                    "notification_id": str(notification.id),
                    "template_id": notification.template_id,
                },
            )
            any_ok = any_ok or ok
        return any_ok
