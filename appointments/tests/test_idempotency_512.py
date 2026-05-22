"""Regression tests for #512 — X-Idempotency-Key on cancel + reschedule.

Acceptance pin (per the design doc Track B gap-fill #4):
- Fresh key → executes normally + stores response.
- Replay with same key + same body → cached response returned, no
  second mutation, no second outbox emit.
- Replay with same key + different body → 422 conflict.
- Expired key (post-TTL) → treated as fresh.
- Cleanup task drops expired rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone as dj_tz
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, IdempotencyKey, OutboxEvent
from appointments.tasks import purge_expired_idempotency_keys
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="idem512_spec", password="x", role="specialist",
        phone="+79991005121",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "512 Specialist"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="512 Cat", slug="512-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="512 Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="idem512_client", password="x", role="client",
        phone="+79991005122",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _confirmed(client_user, specialist, service):
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    # Drop the booking.created outbox row so subsequent assertions
    # only see emits made under test.
    OutboxEvent.objects.filter(
        topic=OutboxEvent.Topic.BOOKING_CREATED,
    ).delete()
    return appt


def _client_as(user, *, idem_key: str | None = None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    if idem_key is not None:
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = idem_key
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestCancelIdempotency:
    """POST /api/v1/appointments/{id}/cancel/ + X-Idempotency-Key."""

    def test_no_header_passes_through_no_tracking(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        r = _client_as(client_user).post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "no header"}, format="json",
        )
        assert r.status_code == 200
        assert IdempotencyKey.objects.count() == 0, (
            "no header → no idempotency record"
        )

    def test_fresh_key_executes_and_stores_response(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        key = "fresh-cancel-key-512"
        r = _client_as(client_user, idem_key=key).post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "fresh"}, format="json",
        )
        assert r.status_code == 200
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        rec = IdempotencyKey.objects.get(
            user=client_user, operation_name="booking.cancel", key=key,
        )
        assert rec.response_status == 200
        assert rec.response_payload  # serialised AppointmentDetail
        # Outbox row written by the cancel service.
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        ).count() == 1

    def test_replay_same_body_returns_cached_no_double_mutation(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        key = "replay-same-body-512"
        body = {"reason": "replay test"}
        c = _client_as(client_user, idem_key=key)
        first = c.post(f"/api/v1/appointments/{appt.id}/cancel/", body, format="json")
        assert first.status_code == 200
        # Replay.
        second = c.post(f"/api/v1/appointments/{appt.id}/cancel/", body, format="json")
        assert second.status_code == 200
        # Same payload returned.
        assert first.data == second.data
        # Exactly one outbox emit — replay did NOT call the service.
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        ).count() == 1
        # One IdempotencyKey row (unique constraint).
        assert IdempotencyKey.objects.filter(
            user=client_user, operation_name="booking.cancel", key=key,
        ).count() == 1

    def test_replay_different_body_returns_422_conflict(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        key = "conflict-body-512"
        c = _client_as(client_user, idem_key=key)
        first = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "first call"}, format="json",
        )
        assert first.status_code == 200
        # Same key, DIFFERENT body.
        second = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "different body"}, format="json",
        )
        assert second.status_code == 422
        assert second.data["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_expired_key_treated_as_fresh(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        key = "expired-512"
        c = _client_as(client_user, idem_key=key)
        first = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "first"}, format="json",
        )
        assert first.status_code == 200
        # Force-expire by back-dating expires_at.
        IdempotencyKey.objects.filter(
            user=client_user, operation_name="booking.cancel", key=key,
        ).update(expires_at=dj_tz.now() - timedelta(seconds=1))

        # Replay should now treat the key as cache miss and re-execute.
        # The booking is already CANCELLED → service raises
        # InvalidStateTransitionError → 422 (NOT cached 200). Proves
        # the helper did NOT serve cached content.
        second = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            {"reason": "first"}, format="json",
        )
        assert second.status_code == 422
        # The expired record was deleted on cache-miss path.
        assert not IdempotencyKey.objects.filter(
            user=client_user, operation_name="booking.cancel", key=key,
            response_status=200,
        ).exists()

    def test_first_call_errors_replay_returns_cached_error(
        self, client_user, specialist, service,
    ):
        """Stripe-semantic: same key + same body that produced a 422
        on the first call returns the SAME 422 on replay (not re-
        executes). Caches errors too so replay-safety holds on the
        sad path. Addresses Code Reviewer #143 blocker.
        """
        appt = _confirmed(client_user, specialist, service)
        # Pre-flip to CANCELLED so the cancel service errors out.
        appt.status = Appointment.Status.CANCELLED
        appt.save(update_fields=["status"])

        key = "error-replay-512"
        c = _client_as(client_user, idem_key=key)
        body = {"reason": "test error replay"}
        first = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            body, format="json",
        )
        # First call errors with 422 (state machine rejects).
        assert first.status_code == 422

        # Replay returns the SAME cached 422 — no re-execute, no
        # IdempotencyInFlight (placeholder was filled by record_response).
        second = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            body, format="json",
        )
        assert second.status_code == 422
        assert first.data == second.data
        # Exactly one row, with the error status cached.
        rec = IdempotencyKey.objects.get(
            user=client_user, operation_name="booking.cancel", key=key,
        )
        assert rec.response_status == 422

    def test_in_flight_placeholder_returns_409(
        self, client_user, specialist, service,
    ):
        """Manually-injected placeholder (response_status=0) — simulates
        a concurrent first-call still executing OR a crashed first
        call. Replay returns 409 IDEMPOTENCY_IN_FLIGHT.
        """
        appt = _confirmed(client_user, specialist, service)
        key = "in-flight-512"
        body = {"reason": "in flight"}
        # Compute the canonical body hash the way the helper does.
        from appointments.infrastructure.idempotency import _hash_body
        body_hash = _hash_body(body)
        IdempotencyKey.objects.create(
            user=client_user,
            key=key,
            operation_name="booking.cancel",
            request_body_hash=body_hash,
            response_status=0,           # placeholder
            response_payload={},
            expires_at=dj_tz.now() + timedelta(hours=12),
        )
        c = _client_as(client_user, idem_key=key)
        r = c.post(
            f"/api/v1/appointments/{appt.id}/cancel/",
            body, format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "IDEMPOTENCY_IN_FLIGHT"
        # Appointment state untouched — operation didn't run.
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


@pytest.mark.django_db
class TestRescheduleIdempotency:
    """Same shape but on the reschedule endpoint — confirms the helper
    is wired identically. One smoke test is enough; full matrix is on
    cancel."""

    def test_replay_same_body_returns_cached_no_double_mutation(
        self, client_user, specialist, service,
    ):
        appt = _confirmed(client_user, specialist, service)
        key = "reschedule-replay-512"
        new_start = _future_utc(5).isoformat()
        body = {"new_start_datetime": new_start}
        c = _client_as(client_user, idem_key=key)
        first = c.post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            body, format="json",
        )
        assert first.status_code == 200
        second = c.post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            body, format="json",
        )
        assert second.status_code == 200
        assert first.data == second.data
        # Exactly one reschedule outbox emit — replay did NOT call service.
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_RESCHEDULED,
        ).count() == 1


@pytest.mark.django_db
class TestCleanupTask:
    """purge_expired_idempotency_keys Celery task drops expired rows."""

    def test_purge_only_deletes_expired(self, client_user):
        now = dj_tz.now()
        # Fresh — should survive.
        fresh = IdempotencyKey.objects.create(
            user=client_user,
            key="fresh",
            operation_name="booking.cancel",
            request_body_hash="x" * 64,
            response_status=200,
            response_payload={},
            expires_at=now + timedelta(hours=12),
        )
        # Expired — should die.
        expired = IdempotencyKey.objects.create(
            user=client_user,
            key="expired",
            operation_name="booking.cancel",
            request_body_hash="y" * 64,
            response_status=200,
            response_payload={},
            expires_at=now - timedelta(hours=1),
        )

        result = purge_expired_idempotency_keys()
        assert result["deleted"] == 1
        assert IdempotencyKey.objects.filter(pk=fresh.pk).exists()
        assert not IdempotencyKey.objects.filter(pk=expired.pk).exists()
