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

Not every message on this path is Ayla's to send (DRF-1030)
------------------------------------------------------------

Some templates name a message bot-platform delivers over MAX. Ayla has
no transport for that channel and no way to acquire one — see
``notifications/delivery_routes.py`` for why. Those rows are terminal
the moment they are written: they record the handoff, never a send, and
never enter the Celery path. Ops reads them by status —
``handed_off`` (passed to the owner, whose own log holds the delivery
fact), ``failed`` with an error naming the unpublished topic (nobody was
told, and the fix is one env var).
"""
from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from .. import delivery_routes, templates
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
        """Render the template and persist a Notification row.

        Two outcomes, decided by who owns delivery of this template
        (``notifications/delivery_routes.py``):

        * **Ayla sends it** — row lands ``PENDING`` and a Celery task is
          queued to push / SMS it.
        * **bot-platform sends it over MAX** — row lands terminal
          (``handed_off``, or ``failed`` when the topic is not published
          in this environment) and nothing is queued. Ayla has no
          transport for MAX; queueing a push here is what produced 45
          fabricated failures on the pilot.

        Raises ``KeyError`` for an unknown template_id — that's a
        programmer bug, not a runtime condition.
        """
        template = templates.get_template(template_id)
        rendered = template.render(context)

        # DRF-1030 — messages bot-platform delivers over MAX never enter
        # the Celery delivery path. Ayla cannot send them and must not
        # file the resulting push failure as though the person heard
        # nothing: on the pilot that mislabelled 45 delivered messages
        # out of 70 as ``failed``.
        route = delivery_routes.bot_route_for(template_id)
        if route is not None:
            return self._record_handoff(
                user=user,
                template_id=template_id,
                rendered=rendered,
                context=context,
                route=route,
            )

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

    def _record_handoff(
        self,
        *,
        user,
        template_id: str,
        rendered: dict[str, Any],
        context: dict[str, Any],
        route: delivery_routes.BotRoute,
    ) -> Notification:
        """Persist the Ayla-side record for a message bot-platform sends.

        The row is terminal on creation — no Celery task, no transport
        attempt — and says only what Ayla can prove:

        * ``channel=max`` — the transport, named honestly, even though
          Ayla is not the one driving it.
        * ``status=handed_off`` — the message was passed to the owner.
          Corroborating evidence is the ``OutboxEvent`` for
          ``data['delivery_topic']`` and its ``bot_delivery_status``;
          the two are joinable by topic + appointment_id.
        * ``status=failed`` with a route-naming error when the topic is
          not published in this environment. Then the handoff did not
          happen and saying it did would be the exact defect this ticket
          exists to remove.

        ``sent_at`` stays NULL on purpose: it means "Ayla sent it", and
        Ayla did not. The rendered title/body are kept as the record of
        what Ayla *intended* to say — bot-platform writes its own copy,
        so these are an audit of intent, not a transcript of what the
        person read.
        """
        live = delivery_routes.topic_is_published(route.topic)
        notification = Notification.objects.create(
            user=user,
            template_id=template_id,
            channel=Notification.Channel.MAX,
            title=rendered["title"],
            body=rendered["body"],
            # ``delivered_by`` / ``delivery_topic`` are what let ops go
            # from "was this person told?" to the outbox row that carries
            # the answer, without reading this module. The sender's
            # file:function citation stays out of ``data`` on purpose —
            # this dict is serialised verbatim by the notifications API
            # (notifications/serializers.py), and bot-platform's internal
            # layout is not the mobile client's business.
            data={
                **context,
                "delivered_by": "bot-platform",
                "delivery_topic": route.topic,
            },
            deep_link=rendered["deep_link"],
            status=(
                Notification.Status.HANDED_OFF if live
                else Notification.Status.FAILED
            ),
            error="" if live else delivery_routes.no_route_error(route),
        )
        if live:
            logger.info(
                "notification.handed_off template=%s topic=%s user_id=%s id=%s",
                template_id, route.topic, getattr(user, "id", None),
                notification.id,
            )
        else:
            # ERROR, not WARNING: in this state the product silently
            # tells nobody anything, and the only way that becomes
            # visible is if someone is paged about it (DRF-1074).
            logger.error(
                "notification.no_route template=%s topic=%s user_id=%s — %s",
                template_id, route.topic, getattr(user, "id", None),
                notification.error,
            )
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
        back to SMS when push fails or the user has no active token.

        MAX has no branch that can return True: Ayla does not own that
        transport, and a row that reaches here on it is a bug upstream,
        not a delivery attempt."""
        from ..services.push import PushService
        from users.sms import SMSService

        channel = notification.channel
        Channel = Notification.Channel

        if channel == Channel.MAX:
            # Defensive: rows on this channel are terminal at creation
            # (see _record_handoff) and never reach here. If one does,
            # something re-queued it — say so rather than inventing a
            # transport Ayla does not have.
            notification.error = (
                "MAX is delivered by bot-platform; Ayla has no transport "
                "for this channel and must not report a send"
            )
            logger.error(
                "notification.max_row_entered_delivery id=%s template=%s",
                notification.id, notification.template_id,
            )
            return False

        if channel in (Channel.PUSH, Channel.BOTH):
            tokens = self._active_tokens_for(notification)
            push_ok = False
            if tokens:
                push_ok = self._send_push_to_tokens(
                    tokens, notification, PushService(),
                )
                # The transport had somewhere to go and refused. That is
                # a blip worth a retry.
                push_error = (
                    ""
                    if push_ok
                    else (
                        "push rejected by transport for all "
                        f"{len(tokens)} registered device(s)"
                    )
                )
            else:
                # The recipient has no app. That is not a failed send —
                # it is a missing channel, and it does not get better on
                # retry. One string for both cases is what let 51 rows
                # read as a single transport problem for two weeks.
                app_type = templates.get_template(
                    notification.template_id,
                ).app_type
                push_error = (
                    f"no registered device for app_type={app_type} — push "
                    "is not a live route for this recipient"
                )
                logger.info(
                    "notification.no_tokens user_id=%s template=%s",
                    notification.user_id, notification.template_id,
                )
            if push_ok:
                return True
            if channel == Channel.PUSH:
                notification.error = push_error
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
