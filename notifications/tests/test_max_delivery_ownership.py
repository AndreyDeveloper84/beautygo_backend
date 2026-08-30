"""Who owns the fact that a notification reached a person (DRF-1030).

Measured on the live pilot 2026-08-30: 70 ``Notification`` rows, 51
``failed`` with ``"push delivery failed (no tokens or send error)"``,
19 ``pending``, and **zero** ``DeviceToken`` rows in the whole
database. Not one notification was ever delivered by Ayla.

The rows are worse than useless — they are wrong in *both* directions,
and the table cannot tell the two cases apart:

* 45 of the 70 (``appointment_created_client`` ×16,
  ``appointment_created_specialist`` ×16, ``appointment_confirmed_client``
  ×13) name messages that bot-platform **did** put in front of the
  person over MAX — it sends them off ``booking.created`` /
  ``booking.confirmed``, which the pilot already publishes. Ayla files
  them as ``failed``.
* The other 25 name messages that genuinely reached nobody.

Ayla cannot fix this by pushing harder. ``chat_id`` lives only in
bot-platform's ``BotUser`` table, cross-repo DB access is forbidden
(ADR-0009 rule #2), and bot-platform exposes no "send this text to this
user" endpoint — the single inbound surface is the event ingest. So the
party that delivers over MAX is bot-platform, and it is the only party
that can own the delivery fact.

What Ayla owns, and what these tests pin, is the **handoff**: the row
must name who delivers, must not report the failure of a channel that
was never the route, and must never claim a handoff that did not
actually happen on the wire.

Why every negative assertion here sits next to a positive one
---------------------------------------------------------------

``test_wire_probe_sees_a_real_delivery`` is the guard the rest of the
module leans on. The negative claims below ("no push was attempted",
"nothing reached anyone") are only worth something if the same probes,
on the same fixtures, can still see a delivery when one really happens.
Without it, a spy that is broken, unpatched, or pointed at the wrong
symbol reports a clean zero and every other test passes for the wrong
reason — which is exactly how 70 rows of status came to stand in for 70
messages nobody received.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from appointments.infrastructure.outbox import emit_outbox_event
from appointments.infrastructure.outbox.publisher import (
    publish_outbox_events_to_bot,
)
from appointments.models import Appointment, OutboxEvent
from notifications import outbox_handlers
from notifications.models import Notification
from services.models import Service, ServiceCategory
from users.models import Profile, SpecialistProfile, User


# The topics the pilot actually publishes to bot-platform, read off the
# live container 2026-08-14 and recorded in DRF-1074. Tests pin this
# exact set rather than the repo default (empty) so they describe the
# environment where people are getting — and not getting — messages.
PILOT_DELIVERY_TOPICS = (
    "booking.created",
    "booking.confirmed",
    "booking.cancelled",
    "appointment.rescheduled",
)

# The contract the fix must satisfy, spelled as literals on purpose:
# these values do not exist on ``Notification`` yet, and a test that
# reached for ``Notification.Channel.MAX`` would blow up with an
# AttributeError instead of showing the status a row actually carries.
CHANNEL_MAX = "max"
STATUS_HANDED_OFF = "handed_off"

# Templates whose message bot-platform puts in front of the person over
# MAX today (verified in ai-bot-platform: apps/booking/client_notify.py
# ``notify_client_booking_confirmed`` and apps/booking/master_notify.py
# ``notify_booking_created``, both fired from
# apps/eventbus/consumers/booking.py).
BOT_OWNED = (
    ("appointment_created_client", "booking.created"),
    ("appointment_created_specialist", "booking.created"),
    ("appointment_confirmed_client", "booking.confirmed"),
)


# ---------------------------------------------------------------------------
# Fixtures — the pilot's shape: real people, zero registered devices.
# ---------------------------------------------------------------------------


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Cat max-route", slug="cat-max-route")


@pytest.fixture
def specialist(db):
    user = User.objects.create_user(
        username="mx-spec", password="x", role="specialist",
        phone="+79994110000",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = "Елена Мастер"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def service(db, specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category,
        name="Маникюр", price="1500.00",
        duration_minutes=60, is_active=True,
    )


@pytest.fixture
def client_user(db):
    user = User.objects.create_user(
        username="mx-client", password="x", role="client",
        phone="+79994220000",
    )
    Profile.objects.filter(user=user).update(full_name="Анна Иванова")
    return user


@pytest.fixture
def appointment(db, client_user, specialist, service):
    # Offsets from now — never a literal date, so the fixture does not
    # rot into the past and drag the reminder window with it.
    when = timezone.now() + timezone.timedelta(hours=26)
    return Appointment.objects.create(
        client=client_user, specialist=specialist, service=service,
        start_datetime=when,
        end_datetime=when + timezone.timedelta(minutes=service.duration_minutes),
        price=service.price,
        status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )


@pytest.fixture
def pilot_topics(settings):
    """Publish exactly what the pilot publishes."""
    settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = PILOT_DELIVERY_TOPICS
    return PILOT_DELIVERY_TOPICS


@pytest.fixture
def bot_reachable(settings):
    """Give the publisher a target so a handoff can actually go out."""
    settings.BOT_PLATFORM_BASE_URL = "https://bot.example.test"
    settings.AYLA_INTERNAL_API_TOKEN = "test-token"  # pragma: allowlist secret
    settings.AYLA_OUTBOUND_HMAC_SECRET = "test-secret"  # pragma: allowlist secret


@pytest.fixture
def push_probe():
    """Records every push Ayla actually attempts to put on the wire.

    Patched at the class the dispatcher instantiates, so a call reaches
    the probe whether or not firebase-admin is installed.
    """
    with patch(
        "notifications.services.push.PushService.send", return_value=True,
    ) as probe:
        yield probe


@pytest.fixture
def wire_probe():
    """Records every HTTP POST the outbox publisher puts on the wire."""
    class _Response:
        status_code = 200
        text = "ok"

    with patch(
        "appointments.infrastructure.outbox.publisher.requests.post",
        return_value=_Response(),
    ) as probe:
        yield probe


@pytest.fixture(autouse=True)
def _no_celery_dispatch():
    """Assert on persisted rows, not on a live worker."""
    with patch("notifications.tasks.deliver_notification.delay") as delay:
        yield delay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(topic: str, appointment, **extra) -> OutboxEvent:
    """Emit through the production helper so the per-topic delivery gate
    decides ``external_delivery_enabled`` exactly as it does on the pilot."""
    return emit_outbox_event(
        topic=topic,
        data={
            "appointment_id": str(appointment.id),
            "client_id": str(appointment.client_id),
            "specialist_id": str(appointment.specialist_id),
            **extra,
        },
        user_id=appointment.client_id,
        tenant_id=str(appointment.tenant_id) if appointment.tenant_id else None,
    )


def _settle(notification: Notification) -> Notification:
    """Run the delivery step the Celery task would run, then re-read."""
    from notifications.services.dispatcher import NotificationService

    if notification.status == Notification.Status.PENDING:
        NotificationService().deliver(notification)
    notification.refresh_from_db()
    return notification


def _row(user, template_id) -> Notification:
    return Notification.objects.get(user=user, template_id=template_id)


# ---------------------------------------------------------------------------
# The positive guard. Everything else in this module is a claim that
# something did NOT happen; this is the claim that the probes can still
# see something that DID.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProbesAreNotVacuous:
    def test_wire_probe_sees_a_real_delivery(
        self, appointment, pilot_topics, bot_reachable, wire_probe,
    ):
        """A published topic reaches bot-platform over HTTP, and the probe
        sees it. Without this, every "nothing was delivered" assertion in
        this module could be a broken patch reporting a clean zero."""
        event = _emit(OutboxEvent.Topic.BOOKING_CREATED, appointment)
        assert event.external_delivery_enabled is True, (
            "booking.created is on the pilot allowlist — if the gate says "
            "otherwise the rest of this module is testing a fiction"
        )

        summary = publish_outbox_events_to_bot()

        assert wire_probe.call_count == 1, "no HTTP request left the process"
        assert summary.sent == 1
        event.refresh_from_db()
        assert event.bot_delivery_status == "sent"
        assert event.bot_delivered_at is not None

    def test_push_probe_sees_a_real_push(self, client_user, push_probe):
        """Give the same dispatcher a device and the push probe fires.

        So when a later test finds zero push attempts, that is a fact
        about the pilot's missing devices, not about a dead patch.
        """
        from notifications.services.dispatcher import NotificationService
        from users.models import DeviceToken

        DeviceToken.objects.create(
            user=client_user, token="tok-1", app_type="client",
            platform="ios", is_active=True,
        )
        notification = Notification.objects.create(
            user=client_user,
            template_id="beauty_insight",
            channel=Notification.Channel.PUSH,
            title="t", body="b", data={"insight_text": "x"},
            status=Notification.Status.PENDING,
        )
        NotificationService().deliver(notification)

        assert push_probe.call_count == 1
        notification.refresh_from_db()
        assert notification.status == Notification.Status.SENT


# ---------------------------------------------------------------------------
# The defect.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRowNamesWhoDelivers:
    """A row must name the party that owns delivery of this message."""

    @pytest.mark.parametrize("template_id,topic", BOT_OWNED)
    def test_bot_owned_message_is_recorded_as_handed_off(
        self, appointment, pilot_topics, template_id, topic,
    ):
        event = _emit(
            OutboxEvent.Topic.BOOKING_CREATED
            if topic == "booking.created"
            else OutboxEvent.Topic.BOOKING_CONFIRMED,
            appointment,
        )
        if topic == "booking.created":
            outbox_handlers.handle_booking_created(event)
        else:
            outbox_handlers.handle_booking_confirmed(event)

        recipient = (
            appointment.specialist.user
            if template_id.endswith("_specialist")
            else appointment.client
        )
        row = _settle(_row(recipient, template_id))

        assert row.channel == CHANNEL_MAX, (
            f"{template_id} is delivered over MAX by bot-platform; the row "
            f"claims channel={row.channel!r}, a channel Ayla cannot send on"
        )
        assert row.status == STATUS_HANDED_OFF, (
            f"{template_id} was handed to bot-platform, which did deliver "
            f"it; the row claims status={row.status!r} error={row.error!r}"
        )
        assert "push" not in row.error.lower(), (
            "the row blames a transport that was never the delivery route"
        )
        assert row.data.get("delivery_topic") == topic, (
            "the row must name the topic whose consumer delivers it, so ops "
            "can join it to the OutboxEvent that carries the handoff"
        )

    def test_bot_owned_message_is_never_queued_for_push(
        self, appointment, pilot_topics, push_probe, _no_celery_dispatch,
    ):
        """Ayla must not also try to send what bot-platform is sending —
        a second attempt is either a duplicate in front of the person or
        a fabricated failure in the table. Here it is the latter."""
        event = _emit(OutboxEvent.Topic.BOOKING_CREATED, appointment)
        outbox_handlers.handle_booking_created(event)

        assert _no_celery_dispatch.call_count == 0, (
            "a delivery task was queued for a message Ayla does not deliver"
        )
        assert push_probe.call_count == 0


@pytest.mark.django_db
class TestHandoffIsBackedByTheWire:
    """``handed_off`` must never be a word the table gives itself."""

    def test_handed_off_row_has_a_real_handoff_behind_it(
        self, appointment, pilot_topics, bot_reachable, wire_probe,
    ):
        event = _emit(OutboxEvent.Topic.BOOKING_CREATED, appointment)
        outbox_handlers.handle_booking_created(event)
        row = _settle(_row(appointment.client, "appointment_created_client"))
        assert row.status == STATUS_HANDED_OFF

        publish_outbox_events_to_bot()

        assert wire_probe.call_count == 1, (
            "the row says the message was handed to bot-platform, but "
            "nothing left the process"
        )
        event.refresh_from_db()
        assert event.bot_delivery_status == "sent", (
            "the handoff Ayla claims must be the one the outbox recorded"
        )

    def test_no_live_route_is_named_as_such(self, appointment, settings):
        """A fresh environment publishes nothing (DRF-1074: the allowlist
        defaults to empty). Then bot-platform never hears about the
        booking and nobody is told. The row has to say *that* — not
        blame a push channel that was never going to carry it."""
        settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = ()
        event = _emit(OutboxEvent.Topic.BOOKING_CREATED, appointment)
        assert event.external_delivery_enabled is False

        outbox_handlers.handle_booking_created(event)
        row = _settle(_row(appointment.client, "appointment_created_client"))

        assert row.status == Notification.Status.FAILED
        assert "booking.created" in row.error, (
            "the error must name the topic that is not being published, so "
            "the fix is one env var away instead of a two-day hunt; got "
            f"{row.error!r}"
        )


@pytest.mark.django_db
class TestPushFailureNamesWhatIsMissing:
    """The 25 rows nobody delivers must say why."""

    def test_missing_device_is_not_reported_as_a_send_error(self, client_user):
        """``"push delivery failed (no tokens or send error)"`` cannot
        distinguish "this person has no app" from "Firebase rejected the
        message". The first is the pilot's permanent condition and needs
        a different channel; the second is a transport blip worth a
        retry. One string for both is what made the audit read 51
        identical failures as one problem."""
        from notifications.services.dispatcher import NotificationService

        notification = Notification.objects.create(
            user=client_user,
            template_id="beauty_insight",
            channel=Notification.Channel.PUSH,
            title="t", body="b", data={"insight_text": "x"},
            status=Notification.Status.PENDING,
        )
        NotificationService().deliver(notification)
        notification.refresh_from_db()

        assert notification.status == Notification.Status.FAILED
        assert "no registered device" in notification.error.lower(), (
            "the row must say the recipient has no device, not that a send "
            f"attempt failed; got {notification.error!r}"
        )
