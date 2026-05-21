"""End-to-end outbox integration test — closes #425 acceptance #2.

Existing tests cover the two halves separately:

- ``test_services.py`` — booking service emits an OutboxEvent row.
- ``test_tasks.py`` — dispatcher marks pending events processed.

This file proves the full chain in one shot: create a booking via the
real application service → observe the OutboxEvent that lands → run
the dispatcher → assert the row is marked processed. If anyone breaks
the wiring between the domain layer and the dispatcher (e.g. forgets
to call ``OutboxEvent.objects.create`` inside the booking transaction,
or registers a handler under the wrong topic), this test fails before
prod sees stale state.

CELERY_TASK_ALWAYS_EAGER=True via settings.test means the dispatcher
runs synchronously here — no broker, no worker process. CI's Postgres
service container gives `select_for_update(skip_locked=True)` real
semantics instead of SQLite's permissive emulation (issue #422).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import OutboxEvent
from appointments.tasks import dispatch_outbox_events
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


# Local fixtures — same shape as test_services.py to keep the booking
# service's preconditions explicit. Shared fixtures would couple this
# file to test_services.py's lifecycle and obscure the end-to-end intent.

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="e2e_specialist",
        password="pass",
        role="specialist",
        phone="+79991002233",
    )


@pytest.fixture
def specialist(specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = "E2E Specialist"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(
        name="E2E Outbox Cat", slug="e2e-outbox-cat",
    )


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="E2E Outbox Service",
        price="1500.00",
        duration_minutes=45,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="e2e_client",
        password="pass",
        role="client",
        phone="+79991002244",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


@pytest.mark.django_db
class TestOutboxEndToEnd:
    """create Appointment → assert OutboxEvent created → dispatcher → processed."""

    def test_booking_flows_through_outbox_to_dispatcher(
        self, client_user, specialist, service,
    ):
        # Pre-state: zero outbox rows. Catches a leaked fixture if any
        # surrounding test forgot to clean up.
        assert OutboxEvent.objects.count() == 0

        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=str(uuid4()),
        )
        CreateBookingService().execute(dto)

        # Half 1: the booking service emitted the outbox row in the
        # same DB transaction as the Appointment. Pending, no errors.
        pending = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CREATED,
        )
        assert pending.count() == 1
        event = pending.first()
        assert event.processed_at is None
        assert event.error_count == 0

        # Half 2: dispatcher picks up the row and marks it processed.
        # In real prod this runs every 10s via the Celery beat schedule
        # in settings/base.py CELERY_BEAT_SCHEDULE["dispatch-outbox-events"];
        # here we drive it directly under CELERY_TASK_ALWAYS_EAGER.
        result = dispatch_outbox_events()
        assert result["processed"] >= 1
        assert result["failed"] == 0

        event.refresh_from_db()
        assert event.processed_at is not None
        assert event.error_count == 0
