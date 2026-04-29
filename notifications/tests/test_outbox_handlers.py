"""Tests for notifications.outbox_handlers — booking events → notifications.

Each handler reads an OutboxEvent + reloads Appointment, then calls
NotificationService.send() once per recipient. We patch
deliver_notification.delay() so the Celery task isn't actually queued —
the assertion is on persisted Notification rows + the right template_id
per recipient.

Templates not yet shipped (Slice N4 fills) are silently skipped — the
handler logs and moves on so a missing template doesn't make the outbox
dispatcher retry the row forever.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from appointments.models import Appointment, OutboxEvent
from notifications import outbox_handlers
from notifications.models import Notification
from services.models import Service, ServiceCategory
from users.models import Profile, SpecialistProfile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Test cat outbox", slug="cat-outbox")


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="ob-spec", password="x", role="specialist",
        phone="+79993110000",
    )


@pytest.fixture
def specialist(db, specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
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
        username="ob-client", password="x", role="client",
        phone="+79993220000",
    )
    Profile.objects.filter(user=user).update(full_name="Анна Иванова")
    return user


@pytest.fixture
def appointment(db, client_user, specialist, service):
    when = timezone.now() + timezone.timedelta(hours=2)
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


def _make_event(topic: str, appointment, **extra) -> OutboxEvent:
    return OutboxEvent.objects.create(
        topic=topic,
        payload={"booking_id": str(appointment.id), **extra},
    )


# Tests run with deliver_notification.delay patched so we don't need
# a Celery worker; we assert on persisted Notification rows.
@pytest.fixture(autouse=True)
def _no_celery_dispatch():
    with patch("notifications.tasks.deliver_notification.delay"):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notif_count(*, user, template_id) -> int:
    return Notification.objects.filter(
        user=user, template_id=template_id,
    ).count()


# ---------------------------------------------------------------------------
# booking.created
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingCreated:
    def test_sends_to_both_client_and_specialist(
        self, appointment, client_user, specialist_user,
    ):
        event = _make_event(
            OutboxEvent.Topic.BOOKING_CREATED, appointment,
            specialist_id=str(appointment.specialist_id),
        )
        outbox_handlers.handle_booking_created(event)
        assert _notif_count(
            user=client_user, template_id="appointment_created_client",
        ) == 1
        assert _notif_count(
            user=specialist_user,
            template_id="appointment_created_specialist",
        ) == 1

    def test_template_renders_dish_name_and_specialist(
        self, appointment, client_user,
    ):
        event = _make_event(OutboxEvent.Topic.BOOKING_CREATED, appointment)
        outbox_handlers.handle_booking_created(event)
        n = Notification.objects.get(
            user=client_user, template_id="appointment_created_client",
        )
        assert "Маникюр" in n.body
        assert "Елена Мастер" in n.body

    def test_missing_appointment_logs_and_skips(self, db):
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
            payload={"booking_id": "00000000-0000-0000-0000-000000000000"},
        )
        # Doesn't raise — just logs and skips.
        outbox_handlers.handle_booking_created(event)
        assert Notification.objects.count() == 0

    def test_missing_booking_id_logs_and_skips(self, db):
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_CREATED, payload={},
        )
        outbox_handlers.handle_booking_created(event)
        assert Notification.objects.count() == 0


# ---------------------------------------------------------------------------
# booking.cancelled
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingCancelled:
    def test_sends_client_template(self, appointment, client_user):
        event = _make_event(
            OutboxEvent.Topic.BOOKING_CANCELLED, appointment,
            initiator_role="client", refund_percent=100, reason="x",
        )
        outbox_handlers.handle_booking_cancelled(event)
        assert _notif_count(
            user=client_user, template_id="appointment_cancelled",
        ) == 1

    def test_specialist_template_pending_until_n4(
        self, appointment, specialist_user,
    ):
        # The specialist-side template is added in Slice N4. Until then
        # the handler logs "template_pending" and continues — no
        # Notification row, but no exception either.
        event = _make_event(
            OutboxEvent.Topic.BOOKING_CANCELLED, appointment,
            initiator_role="specialist",
        )
        outbox_handlers.handle_booking_cancelled(event)
        # Specialist row is absent because template doesn't exist yet.
        assert _notif_count(
            user=specialist_user,
            template_id="appointment_cancelled_specialist",
        ) == 0


# ---------------------------------------------------------------------------
# booking.rescheduled
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingRescheduled:
    def test_no_rows_until_n4_templates(self, appointment):
        # Both rescheduled templates land in Slice N4. Handler is a
        # no-op today, but it MUST be registered so the topic isn't
        # an unknown_topic error.
        event = _make_event(
            OutboxEvent.Topic.BOOKING_RESCHEDULED, appointment,
            old_start_at="2026-04-30T10:00:00Z",
        )
        outbox_handlers.handle_booking_rescheduled(event)
        assert Notification.objects.count() == 0

    def test_old_start_at_parsed_into_context_when_template_exists(
        self, appointment, client_user, monkeypatch,
    ):
        # Inject a temporary template into the registry so we can assert
        # that old_date_time is rendered. The template added here mirrors
        # the shape Slice N4 will ship.
        from notifications import templates as tmpl_mod

        tmpl_mod.TEMPLATES["appointment_rescheduled_client"] = (
            tmpl_mod.NotificationTemplate(
                id="appointment_rescheduled_client",
                app_type="client",
                channel=Notification.Channel.PUSH,
                push_title="Запись перенесена",
                push_body="С {old_date_time} на {date_time}",
                deep_link="appointment/{appointment_id}",
            )
        )
        try:
            event = _make_event(
                OutboxEvent.Topic.BOOKING_RESCHEDULED, appointment,
                old_start_at="2026-04-30T10:00:00+00:00",
            )
            outbox_handlers.handle_booking_rescheduled(event)
            n = Notification.objects.get(
                user=client_user,
                template_id="appointment_rescheduled_client",
            )
            assert "10:00 30.04" in n.body
        finally:
            tmpl_mod.TEMPLATES.pop("appointment_rescheduled_client", None)


# ---------------------------------------------------------------------------
# booking.completed / booking.no_show / booking.confirmed (no writers yet)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFutureTopics:
    def test_completed_is_noop_without_template(self, appointment):
        event = _make_event(OutboxEvent.Topic.BOOKING_COMPLETED, appointment)
        outbox_handlers.handle_booking_completed(event)
        assert Notification.objects.count() == 0

    def test_no_show_is_noop_without_template(self, appointment):
        event = _make_event(OutboxEvent.Topic.BOOKING_NO_SHOW, appointment)
        outbox_handlers.handle_booking_no_show(event)
        assert Notification.objects.count() == 0

    def test_confirmed_is_noop_without_template(self, appointment):
        event = _make_event(OutboxEvent.Topic.BOOKING_CONFIRMED, appointment)
        outbox_handlers.handle_booking_confirmed(event)
        assert Notification.objects.count() == 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_booking_handlers_registry_covers_all_booking_topics():
    """All 6 booking topics in OutboxEvent.Topic must be in BOOKING_HANDLERS."""
    booking_topics = {
        OutboxEvent.Topic.BOOKING_CREATED,
        OutboxEvent.Topic.BOOKING_CONFIRMED,
        OutboxEvent.Topic.BOOKING_CANCELLED,
        OutboxEvent.Topic.BOOKING_RESCHEDULED,
        OutboxEvent.Topic.BOOKING_COMPLETED,
        OutboxEvent.Topic.BOOKING_NO_SHOW,
    }
    assert booking_topics <= outbox_handlers.BOOKING_HANDLERS.keys()


def test_booking_handlers_registered_in_event_handlers():
    """appointments.tasks.EVENT_HANDLERS must have the booking handlers
    bound (no longer the _log_handler stubs)."""
    from appointments import tasks as apt_tasks

    for topic, handler in outbox_handlers.BOOKING_HANDLERS.items():
        assert apt_tasks.EVENT_HANDLERS[topic] is handler


def test_payment_handlers_registered():
    """N2 wires payment topics to real handlers — no more _log_handler."""
    from appointments import tasks as apt_tasks

    assert (
        apt_tasks.EVENT_HANDLERS[OutboxEvent.Topic.PAYMENT_CONFIRMED]
        is outbox_handlers.handle_payment_confirmed
    )
    assert (
        apt_tasks.EVENT_HANDLERS[OutboxEvent.Topic.PAYMENT_REFUNDED]
        is outbox_handlers.handle_payment_refunded
    )


# ---------------------------------------------------------------------------
# payment.confirmed / payment.refunded
# ---------------------------------------------------------------------------


@pytest.fixture
def payment(db, appointment):
    from appointments.models import Payment

    return Payment.objects.create(
        appointment=appointment,
        amount=appointment.price,
        status=Payment.Status.PAID,
        specialist_income=appointment.price,
        platform_fee="0",
        provider="yookassa",
    )


@pytest.mark.django_db
class TestPaymentConfirmed:
    def test_sends_payment_paid_to_client(self, payment, client_user):
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.PAYMENT_CONFIRMED,
            payload={
                "payment_id": str(payment.id),
                "appointment_id": str(payment.appointment_id),
                "amount": str(payment.amount),
            },
        )
        outbox_handlers.handle_payment_confirmed(event)
        rows = Notification.objects.filter(
            user=client_user, template_id="payment_paid",
        )
        assert rows.count() == 1
        n = rows.first()
        assert "Маникюр" in n.body
        assert str(payment.amount) in n.body or "1500" in n.body

    def test_missing_payment_logs_and_skips(self, db):
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.PAYMENT_CONFIRMED,
            payload={
                "payment_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        outbox_handlers.handle_payment_confirmed(event)
        assert Notification.objects.count() == 0

    def test_missing_payment_id_logs_and_skips(self, db):
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.PAYMENT_CONFIRMED, payload={},
        )
        outbox_handlers.handle_payment_confirmed(event)
        assert Notification.objects.count() == 0


@pytest.mark.django_db
class TestPaymentRefunded:
    def test_template_pending_no_notification_until_n4(self, payment):
        # `payment_refunded` template lands in Slice N4. Until then the
        # handler is a logged no-op.
        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.PAYMENT_REFUNDED,
            payload={
                "payment_id": str(payment.id),
                "appointment_id": str(payment.appointment_id),
                "amount": str(payment.amount),
                "is_partial": False,
            },
        )
        outbox_handlers.handle_payment_refunded(event)
        assert Notification.objects.count() == 0
