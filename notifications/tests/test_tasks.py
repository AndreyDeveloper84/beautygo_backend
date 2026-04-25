"""Tests for the Celery beat reminder dispatcher."""
import datetime as dt

import pytest
from django.utils import timezone

from appointments.models import Appointment
from notifications.models import Notification
from notifications.tasks import (
    REMINDER_LEAD_MINUTES,
    REMINDER_TEMPLATE_ID,
    dispatch_appointment_reminders,
)
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="rclient", password="x", role="client", phone="+79993330000",
    )


@pytest.fixture
def specialist_profile(db):
    user = User.objects.create_user(
        username="rspec", password="x", role="specialist", phone="+79994440000",
    )
    profile = user.specialist_profile
    profile.display_name = "Reminder Master"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.address = "Some St 1"
    profile.save(update_fields=["display_name", "status", "address"])
    return profile


@pytest.fixture
def service(db, specialist_profile):
    cat = ServiceCategory.objects.create(name="cat", slug="cat")
    return Service.objects.create(
        specialist=specialist_profile,
        category=cat,
        name="Маникюр",
        price=1000,
        duration_minutes=60,
    )


def _confirmed_appointment(client, specialist_profile, service, start_offset_min):
    return Appointment.objects.create(
        client=client,
        specialist=specialist_profile,
        service=service,
        start_datetime=timezone.now() + dt.timedelta(minutes=start_offset_min),
        end_datetime=timezone.now() + dt.timedelta(minutes=start_offset_min + 60),
        status=Appointment.Status.CONFIRMED,
        price=service.price,
    )


@pytest.mark.django_db
class TestDispatchReminders:
    def test_appointment_in_window_creates_reminder(
        self, client_user, specialist_profile, service,
    ):
        appt = _confirmed_appointment(
            client_user, specialist_profile, service, REMINDER_LEAD_MINUTES,
        )
        result = dispatch_appointment_reminders()
        assert result["queued"] == 1
        assert Notification.objects.filter(
            user=client_user,
            template_id=REMINDER_TEMPLATE_ID,
            data__appointment_id=str(appt.id),
        ).exists()

    def test_appointment_outside_window_skipped(
        self, client_user, specialist_profile, service,
    ):
        # 2 hours out — well past the [55, 65] window
        _confirmed_appointment(
            client_user, specialist_profile, service, 120,
        )
        result = dispatch_appointment_reminders()
        assert result["queued"] == 0

    def test_pending_appointment_not_reminded(
        self, client_user, specialist_profile, service,
    ):
        appt = _confirmed_appointment(
            client_user, specialist_profile, service, REMINDER_LEAD_MINUTES,
        )
        appt.status = Appointment.Status.PENDING
        appt.save(update_fields=["status"])
        result = dispatch_appointment_reminders()
        assert result["queued"] == 0

    def test_double_run_does_not_duplicate(
        self, client_user, specialist_profile, service,
    ):
        _confirmed_appointment(
            client_user, specialist_profile, service, REMINDER_LEAD_MINUTES,
        )
        first = dispatch_appointment_reminders()
        second = dispatch_appointment_reminders()
        assert first["queued"] == 1
        assert second["queued"] == 0
        assert second["skipped"] == 1
        assert Notification.objects.count() == 1
