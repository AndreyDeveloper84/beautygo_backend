"""Who delivers which notification — the one place that answers it.

Ayla can put a message in front of a person over exactly two transports
it owns: FCM push and SMS. MAX is not one of them, and cannot become
one:

* the channel address (``BotUser.chat_id``) lives only in
  bot-platform's database, and ADR-0009 forbids cross-repo DB access;
* bot-platform exposes no "send this text to this user" endpoint — its
  single inbound surface is ``POST /api/v1/internal/events/ingest``;
* the copy, the consent gate (``apps/notifications/proactive.py``) and
  the quiet-hours policy are bot-platform's domain per the ADR-0009
  ownership matrix ("MAX, Telegram, future WhatsApp channels").

So for the messages listed below Ayla is not the sender. It emits a
domain event; bot-platform's consumer renders its own copy and sends it.
**bot-platform owns the fact of delivery. Ayla owns the fact of
handoff** — and this module exists so that distinction is a declared
fact in one file rather than an assumption spread across handlers.

Nothing here changes what goes on the wire. The events were already
being published (``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` on the pilot
carries ``booking.created`` and ``booking.confirmed``) and bot-platform
was already sending these three messages. What was missing is that Ayla
*also* queued a push for them and then filed the inevitable failure as
though the person had been told nothing — 45 of the 70 rows measured on
2026-08-30.

What a handoff does and does not promise
----------------------------------------

``handed_off`` says the message reached the party that delivers it. It
does not say a person read something, and it must never be read that
way — bot-platform applies its own suppression on top:
``notify_client_booking_confirmed`` stays quiet when the booking is
still pending (the prepayment path is covered later by
``booking.confirmed``) and when the booking was made in the chat dialog,
where the person has already seen the confirmation in front of them.
Those are the owner's decisions to make, and they are the reason Ayla
cannot answer "did this person hear from us?" from its own tables. The
answer lives in bot-platform.

Adding a template here is a cross-repo claim
--------------------------------------------

An entry asserts that a specific bot-platform function delivers this
message today. Each one names that function so the claim can be
re-checked rather than trusted. Do not add a template on the strength
of "the bot consumes that topic": consuming an event and messaging a
human are different things, and for ``booking.cancelled``,
``appointment.rescheduled``, ``booking.completed`` and
``booking.no_show`` the consumer today only re-pegs reminders and
mirrors state — nobody is told. Those templates stay off this map on
purpose, so their rows keep saying, truthfully, that no one delivered
them.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class BotRoute:
    """A message bot-platform delivers, and the evidence for saying so."""

    #: OutboxEvent topic whose bot-side consumer sends the message. The
    #: topic must be on ``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` or the event
    #: never leaves Ayla and nobody is told anything.
    topic: str
    #: Fully-qualified bot-platform sender, for the human re-checking
    #: this claim. Not imported, not called — a citation.
    sender: str


# Verified against ai-bot-platform on 2026-08-30. The consumer is
# apps/eventbus/consumers/booking.py; the senders are the two functions
# named below, both dispatched via apps/handoff/notify.send_max_notification.
BOT_OWNED_TEMPLATES: dict[str, BotRoute] = {
    # "✅ Вы записаны / Услуга / Мастер / Когда / Салон" — sent when the
    # booking arrives already confirmed and did not originate in the
    # chat dialog (the bot does not repeat itself to someone who just
    # booked in the conversation).
    "appointment_created_client": BotRoute(
        topic="booking.created",
        sender="apps/booking/client_notify.py::notify_client_booking_confirmed",
    ),
    # "🆕 Новая запись / Салон / Услуга / Мастер / Когда / Источник".
    # Recipient resolution walks the master's linked BotUser, then the
    # tenant's manager chat, then the ops fallback chat — so the salon
    # learns about the booking even when the master has never opened the
    # bot.
    "appointment_created_specialist": BotRoute(
        topic="booking.created",
        sender="apps/booking/master_notify.py::notify_booking_created",
    ),
    # Same client-facing text, on the transition into confirmed — the
    # prepayment path, where booking.created arrived while the booking
    # was still pending.
    "appointment_confirmed_client": BotRoute(
        topic="booking.confirmed",
        sender="apps/booking/client_notify.py::notify_client_booking_confirmed",
    ),
}


def bot_route_for(template_id: str) -> BotRoute | None:
    """Return the bot-platform route for ``template_id``, or ``None``
    when Ayla is the sender."""
    return BOT_OWNED_TEMPLATES.get(template_id)


def topic_is_published(topic: str) -> bool:
    """Is ``topic`` actually shipped to bot-platform in this environment?

    ``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` defaults to empty, which means a
    fresh stand, a local checkout or a second salon publishes nothing at
    all (DRF-1074). A handoff to a topic nobody publishes is not a
    handoff — it is silence — and the caller records it as such instead
    of claiming the message was passed on.
    """
    allowlist = getattr(settings, "OUTBOX_EXTERNAL_DELIVERY_TOPICS", ()) or ()
    return topic in set(allowlist)


def no_route_error(route: BotRoute) -> str:
    """The error text for a message that has no live route to anyone.

    Names the topic and the env var, because the whole cost of DRF-1074
    was that the failure looked like nothing: no error, no log, a clean
    deploy and a customer who never heard from us.
    """
    return (
        f"no live route: {route.topic} is not in "
        f"OUTBOX_EXTERNAL_DELIVERY_TOPICS, so the event never reaches "
        f"bot-platform and {route.sender} never runs. Nobody was told."
    )
