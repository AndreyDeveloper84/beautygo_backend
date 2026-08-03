"""Wave 1 Simple Reschedule hardening — production-safety tests.

Covers the 10-point gap list from
D:\\Проекты\\Ayla\\AYLA_SIMPLE_RESCHEDULE_AGENT_PACK\\01_AGENT_BE_BACKEND_IMPLEMENTATION.md
that appointments/tests/test_services.py::TestRescheduleBookingService
(the pre-existing suite) does not exercise: post-lock recheck, version/
Revision, PostgreSQL collision protection, dual event emission, internal
endpoint idempotency, cache invalidation, and the new guard set.

Concurrency tests need real cross-connection visibility, so they use
``@pytest.mark.django_db(transaction=True)`` — a plain ``db``-fixture
test runs inside a rolled-back transaction that a second thread's own
connection would never see.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import RescheduleBookingDTO
from appointments.application.services.cancel_reschedule_service import (
    RescheduleBookingService,
)
from appointments.domain.exceptions import (
    AppointmentTerminalError,
    BookingWindowError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from appointments.infrastructure.cache.slot_cache import SlotCacheService
from appointments.models import Appointment, AppointmentRevision, OutboxEvent
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grid_future_utc(hours: int) -> datetime:
    """Grid-aligned future datetime — required by the new common guard
    (BOOKING_SLOT_GRID_MINUTES=30)."""
    dt = (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)
    return dt.replace(minute=dt.minute - (dt.minute % 30))


def _create_confirmed(client_user, specialist, service, *, hours: int = 48):
    start = _grid_future_utc(hours)
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist,
        service=service,
        start_datetime=start,
        end_datetime=start + timedelta(minutes=service.duration_minutes),
        price=service.price,
        status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )


# ---------------------------------------------------------------------------
# Fixtures — separate names from conftest.py's to avoid any cross-test
# tenant/state coupling; mirrors the pattern in test_services.py.
# ---------------------------------------------------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='w1_specialist', password='pass', role='specialist',
        phone='+79990002200',
    )


@pytest.fixture
def specialist(specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = 'Wave1 Specialist'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Wave1 Cat', slug='wave1-cat')


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name='Wave1 Service',
        price='1500.00',
        duration_minutes=60,
        is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='w1_client', password='pass', role='client',
        phone='+79990002201',
    )


# ---------------------------------------------------------------------------
# 1. Happy path — version bump, Revision, dual event emission
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_version_increments_and_revision_written(
        self, client_user, specialist, service,
    ):
        appt = _create_confirmed(client_user, specialist, service)
        assert appt.version == 1
        new_start = _grid_future_utc(72)

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
            command_key="cmd-happy-1",
            basis="mobile_app",
        ))

        appt.refresh_from_db()
        assert appt.version == 2
        assert appt.start_datetime == new_start

        revisions = list(AppointmentRevision.objects.filter(appointment=appt))
        assert len(revisions) == 1
        rev = revisions[0]
        assert rev.version == 2
        assert rev.new_start_datetime == new_start
        assert rev.actor_id == client_user.id
        assert rev.actor_role == "client"
        assert rev.basis == "mobile_app"
        assert rev.command_key == "cmd-happy-1"

    def test_emits_canonical_and_legacy_events_in_one_success(
        self, client_user, specialist, service,
    ):
        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        ))

        canonical = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
        )
        legacy = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_RESCHEDULED,
        )
        # Ayla Domain Event Registry v0.4 §6.3 required payload keys.
        assert canonical.data["appointment_id"] == str(appt.id)
        assert canonical.data["version"] == 2
        assert canonical.data["previous_version"] == 1
        assert "revision_id" in canonical.data
        assert canonical.data["changed_fields"] == ["starts_at"]
        assert canonical.data["actor"] == "user"
        # Registry optional keys used by Wave 1.
        assert canonical.data["starts_at"] == new_start.isoformat()
        assert canonical.data["previous_starts_at"]
        # Legacy shape unchanged — new_start_at hard-key still present.
        assert legacy.data["new_start_at"] == new_start.isoformat()
        assert legacy.data["start_at"] == new_start.isoformat()

    def test_canonical_event_actor_reflects_initiator_role(
        self, client_user, specialist, service,
    ):
        """Registry §6.3 actor enum wants the literal initiator role
        (specialist), not the coarser ADR-0009 envelope actor bucket
        (which collapses specialist -> "admin")."""
        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
            initiator_role="specialist",
        ))

        canonical = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
        )
        assert canonical.data["actor"] == "specialist"


# ---------------------------------------------------------------------------
# 2. Post-lock authoritative recheck
# ---------------------------------------------------------------------------

class TestPostLockRecheck:
    def test_terminal_recheck_rejects_row_cancelled_between_precheck_and_lock(
        self, client_user, specialist, service,
    ):
        """Simulates the race directly: the row is ALREADY terminal by
        the time _execute_atomic runs (as if a concurrent cancel won).
        execute()'s pre-lock check is bypassed on purpose — this proves
        the post-lock check is what actually protects the row, not the
        pre-lock one."""
        appt = _create_confirmed(client_user, specialist, service)
        appt.status = Appointment.Status.CANCELLED
        appt.save(update_fields=["status"])
        new_start = _grid_future_utc(72)

        with pytest.raises(AppointmentTerminalError):
            RescheduleBookingService()._execute_atomic(
                booking_id=appt.id,
                specialist_id=specialist.id,
                new_start_at=new_start,
                initiator_user_id=client_user.id,
            )

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        assert appt.version == 1
        assert not AppointmentRevision.objects.filter(appointment=appt).exists()
        assert not OutboxEvent.objects.filter(
            topic__in=[
                OutboxEvent.Topic.BOOKING_RESCHEDULED,
                OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
            ],
        ).exists()

    def test_stale_version_rejected(self, client_user, specialist, service):
        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)

        with pytest.raises(StaleVersionError):
            RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                new_start_at=new_start,
                expected_version=99,
            ))

        appt.refresh_from_db()
        assert appt.version == 1
        assert appt.start_datetime != new_start

    def test_mobile_stale_version_rejected_via_http(
        self, client_user, specialist, service,
    ):
        """HTTP-boundary counterpart of test_stale_version_rejected —
        exercises the actual views.py error mapping (StaleVersionError
        -> 409 STALE_VERSION), not just the service layer directly."""
        appt = _create_confirmed(client_user, specialist, service)
        r = _mobile_api(client_user).post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 99,
            },
            format="json",
        )
        assert r.status_code == 409, r.data
        assert r.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.version == 1

    def test_internal_stale_version_rejected_via_http(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="internal-stale-version-1")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 99,
            },
            format="json",
        )
        assert r.status_code == 409, r.data
        assert r.data["error"]["code"] == "STALE_VERSION"
        appt.refresh_from_db()
        assert appt.version == 1

    def test_correct_expected_version_succeeds(
        self, client_user, specialist, service,
    ):
        appt = _create_confirmed(client_user, specialist, service)
        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=_grid_future_utc(72),
            expected_version=1,
        ))
        appt.refresh_from_db()
        assert appt.version == 2

    def test_tenant_mismatch_rejected(self, client_user, specialist, service):
        appt = _create_confirmed(client_user, specialist, service)
        wrong_tenant_id = uuid4()
        assert appt.tenant_id != wrong_tenant_id

        with pytest.raises(TenantMismatchError):
            RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                new_start_at=_grid_future_utc(72),
                tenant_id=wrong_tenant_id,
            ))

        appt.refresh_from_db()
        assert appt.version == 1

    def test_none_tenant_id_skips_check(self, client_user, specialist, service):
        """tenant_id=None (the internal/bot path's default) means 'no
        tenant context to check' — must NOT reject."""
        appt = _create_confirmed(client_user, specialist, service)
        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=_grid_future_utc(72),
            tenant_id=None,
        ))
        appt.refresh_from_db()
        assert appt.version == 2


# ---------------------------------------------------------------------------
# 3. Common guards (window / grid / time-off)
# ---------------------------------------------------------------------------

class TestCommonGuards:
    def test_off_grid_time_rejected(self, client_user, specialist, service):
        appt = _create_confirmed(client_user, specialist, service)
        off_grid = _grid_future_utc(72) + timedelta(minutes=7)

        with pytest.raises(BookingWindowError):
            RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                new_start_at=off_grid,
            ))
        appt.refresh_from_db()
        assert appt.version == 1

    def test_horizon_violation_rejected(self, client_user, specialist, service):
        appt = _create_confirmed(client_user, specialist, service)
        too_far = _grid_future_utc(24 * 90)  # BOOKING_MAX_AHEAD_DAYS=60

        with pytest.raises(BookingWindowError):
            RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                new_start_at=too_far,
            ))

    def test_time_off_block_rejected(self, client_user, specialist, service):
        from appointments.models import SpecialistTimeOff

        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)
        SpecialistTimeOff.objects.create(
            specialist=specialist,
            start_at=new_start - timedelta(minutes=30),
            end_at=new_start + timedelta(minutes=90),
            reason="vacation",
        )

        with pytest.raises(SlotNotAvailableError):
            RescheduleBookingService().execute(RescheduleBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                new_start_at=new_start,
            ))
        appt.refresh_from_db()
        assert appt.version == 1


# ---------------------------------------------------------------------------
# 4. PostgreSQL concurrency — advisory lock closes the phantom-insert race
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestConcurrentReschedule:
    def test_two_concurrent_reschedules_to_same_open_slot_serialise(
        self, client_user, specialist, service,
    ):
        """Two appointments for the SAME specialist, both rescheduled
        concurrently to the SAME open target slot. Without the advisory
        lock, select_for_update on the (empty) conflict queryset locks
        nothing, so both could pass the conflict check and both write
        overlapping times. With the lock, exactly one wins."""
        appt_a = _create_confirmed(client_user, specialist, service, hours=48)
        appt_b = _create_confirmed(client_user, specialist, service, hours=96)
        target = _grid_future_utc(200)

        results = {}

        def _reschedule(key, booking_id):
            import django.db

            django.db.connections.close_all()
            try:
                RescheduleBookingService().execute(RescheduleBookingDTO(
                    booking_id=booking_id,
                    initiator_user_id=client_user.id,
                    new_start_at=target,
                ))
                results[key] = "ok"
            except SlotNotAvailableError:
                results[key] = "slot_taken"
            finally:
                django.db.connections.close_all()

        t1 = threading.Thread(target=_reschedule, args=("a", appt_a.id))
        t2 = threading.Thread(target=_reschedule, args=("b", appt_b.id))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        outcomes = sorted(results.values())
        assert outcomes == ["ok", "slot_taken"], results

        appt_a.refresh_from_db()
        appt_b.refresh_from_db()
        # Exactly one of the two now occupies the target slot — no
        # overlapping pair persists.
        winners = [
            a for a in (appt_a, appt_b) if a.start_datetime == target
        ]
        assert len(winners) == 1


# ---------------------------------------------------------------------------
# 5. Internal (bot) endpoint idempotency — reschedule + cancel
# ---------------------------------------------------------------------------

INTERNAL_VALID_TOKEN = "test-ayla-internal-token-w1"
INTERNAL_EXTERNAL_USER_ID = "bot:w1customer"


@pytest.fixture(autouse=True)
def _internal_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = INTERNAL_VALID_TOKEN


def _internal_api(*, idem_key: str | None = None) -> APIClient:
    # Mirrors test_internal_booking_rest_1016.py::_api — resolve_external_user
    # resolves the bearer's X-External-User-ID by matching User.username,
    # so the "customer" fixture below pre-creates a proxy User with that
    # exact username for a deterministic resolution.
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {INTERNAL_VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = INTERNAL_EXTERNAL_USER_ID
    if idem_key is not None:
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = idem_key
    return c


@pytest.fixture
def internal_customer(db):
    return User.objects.create_user(
        username=INTERNAL_EXTERNAL_USER_ID, password="x", role="client",
        phone="+79990002299", is_proxy=True,
    )


class TestInternalEndpointIdempotency:
    """Owner-scoped decision: fix idempotency for BOTH reschedule and
    cancel on the bot-facing internal endpoint — same bug (missing
    X-Idempotency-Key wiring), same fix, real duplicate-mutation risk."""

    def test_reschedule_replay_same_body_no_double_mutation(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        new_start = _grid_future_utc(72)
        api = _internal_api(idem_key="internal-reschedule-key-1")
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"
        body = {
            "new_start_datetime": new_start.isoformat(),
            "expected_version": 1,
        }

        first = api.post(url, body, format="json")
        assert first.status_code == 200, first.data
        second = api.post(url, body, format="json")
        assert second.status_code == 200
        assert first.data == second.data

        appt.refresh_from_db()
        assert appt.version == 2  # not 3 — replay did not re-mutate
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
        ).count() == 1

    def test_reschedule_replay_different_body_conflicts(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="internal-reschedule-key-2")
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"

        first = api.post(
            url,
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert first.status_code == 200, first.data
        second = api.post(
            url,
            {
                "new_start_datetime": _grid_future_utc(96).isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert second.status_code == 422
        assert second.data["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    def test_cancel_replay_same_body_no_double_mutation(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="internal-cancel-key-1")
        url = f"/api/v1/internal/appointments/{appt.id}/cancel/"
        body = {"reason": "changed my mind"}

        first = api.post(url, body, format="json")
        assert first.status_code == 200, first.data
        second = api.post(url, body, format="json")
        assert second.status_code == 200
        assert first.data == second.data
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        ).count() == 1

    def test_cancel_replay_in_flight_placeholder_not_reexecuted(
        self, internal_customer, specialist, service,
    ):
        """Simulates a crash-then-retry: a placeholder row left at
        response_status=0 (as if the first request died mid-flight)
        must return 409 IN_FLIGHT, never silently re-execute."""
        from appointments.infrastructure.idempotency import _hash_body
        from appointments.models import IdempotencyKey

        appt = _create_confirmed(internal_customer, specialist, service)
        body = {"reason": "changed my mind"}
        IdempotencyKey.objects.create(
            user=internal_customer,
            key="internal-cancel-inflight",
            operation_name="booking.cancel.internal",
            target_type="Appointment",
            target_id=str(appt.id),
            request_body_hash=_hash_body(body),
            response_status=0,
            response_payload={},
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        )
        api = _internal_api(idem_key="internal-cancel-inflight")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/cancel/",
            {"reason": "changed my mind"}, format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "IDEMPOTENCY_IN_FLIGHT"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED  # untouched


# ---------------------------------------------------------------------------
# 6. Cache invalidation — old + new date, on the LIVE dispatch path
# ---------------------------------------------------------------------------

class TestCacheInvalidation:
    def test_reschedule_invalidates_both_old_and_new_date_cache(
        self, client_user, specialist, service,
    ):
        from datetime import date as _date

        from appointments.application.dto import DayAvailabilityDTO
        from notifications import outbox_handlers

        appt = _create_confirmed(client_user, specialist, service)
        old_date = appt.start_datetime.date()
        new_start = _grid_future_utc(72)
        new_date = new_start.date()
        assert old_date != new_date

        cache_svc = SlotCacheService()
        dummy = DayAvailabilityDTO(date=_date(2020, 1, 1), is_working_day=True, slots=[])
        cache_svc.set(specialist.id, old_date, service.id, dummy)
        cache_svc.set(specialist.id, new_date, service.id, dummy)
        assert cache_svc.get(specialist.id, old_date, service.id) is not None
        assert cache_svc.get(specialist.id, new_date, service.id) is not None

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        ))
        event = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_RESCHEDULED)
        outbox_handlers.handle_booking_rescheduled(event)

        assert cache_svc.get(specialist.id, old_date, service.id) is None
        assert cache_svc.get(specialist.id, new_date, service.id) is None


# ---------------------------------------------------------------------------
# 7. Targeted patch before commit: shared correlation_id, version +
#    revision_id in the success response, X-Idempotency-Key policy on
#    the internal path (see AGENT_BE_FINAL_TARGETED_PATCH_BEFORE_COMMIT.md).
# ---------------------------------------------------------------------------

def _mobile_api(client_user_obj, *, idem_key: str | None = None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    if idem_key is not None:
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = idem_key
    c.force_authenticate(user=client_user_obj)
    return c


class TestSharedCorrelationId:
    def test_canonical_and_legacy_events_share_correlation_id(
        self, client_user, specialist, service,
    ):
        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)

        RescheduleBookingService().execute(RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        ))

        canonical = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
        )
        legacy = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_RESCHEDULED,
        )
        assert canonical.payload["correlation_id"]
        assert canonical.payload["correlation_id"] == legacy.payload["correlation_id"]
        # Distinct event_id (dedupe key) even though correlation matches.
        assert canonical.payload["event_id"] != legacy.payload["event_id"]

    def test_duplicate_internal_command_does_not_create_new_events(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        new_start = _grid_future_utc(72)
        api = _internal_api(idem_key="internal-corr-dup-1")
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"
        body = {
            "new_start_datetime": new_start.isoformat(),
            "expected_version": 1,
        }

        api.post(url, body, format="json")
        api.post(url, body, format="json")

        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
        ).count() == 1
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_RESCHEDULED,
        ).count() == 1


class TestResponseContract:
    """Success response contains ``version`` + ``revision_id`` matching
    the just-written Appointment/AppointmentRevision (item 2)."""

    def test_mobile_success_response_contains_version_and_revision_id(
        self, client_user, specialist, service,
    ):
        appt = _create_confirmed(client_user, specialist, service)
        new_start = _grid_future_utc(72)
        r = _mobile_api(client_user).post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": new_start.isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert r.status_code == 200, r.data
        assert r.data["data"]["version"] == 2
        rev = AppointmentRevision.objects.get(appointment=appt)
        assert r.data["data"]["revision_id"] == str(rev.id)

    def test_internal_success_response_contains_version_and_revision_id(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        new_start = _grid_future_utc(72)
        api = _internal_api(idem_key="internal-response-contract-1")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": new_start.isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert r.status_code == 200, r.data
        assert r.data["data"]["version"] == 2
        rev = AppointmentRevision.objects.get(appointment=appt)
        assert r.data["data"]["revision_id"] == str(rev.id)

    def test_idempotent_replay_returns_identical_version_and_revision_id(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        new_start = _grid_future_utc(72)
        api = _internal_api(idem_key="internal-response-contract-2")
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"
        body = {
            "new_start_datetime": new_start.isoformat(),
            "expected_version": 1,
        }

        first = api.post(url, body, format="json")
        second = api.post(url, body, format="json")
        assert first.status_code == 200, first.data
        assert first.data == second.data
        assert first.data["data"]["revision_id"] == second.data["data"]["revision_id"]


class TestInternalIdempotencyKeyPolicy:
    """AGENT_BE_ENFORCE_INTERNAL_IDEMPOTENCY_BEFORE_COMMIT.md — X-Idempotency-Key
    is now MANDATORY (not just recommended) for internal reschedule/cancel.
    A missing or blank header is rejected with 400 IDEMPOTENCY_KEY_REQUIRED
    before the application service ever runs: no mutation, no Revision, no
    outbox event. See InternalBookingRescheduleView/InternalBookingCancelView
    ``_require_idempotency_key`` docstring."""

    def test_reschedule_without_key_rejected_no_mutation(
        self, internal_customer, specialist, service,
    ):
        from appointments.models import AppointmentRevision, IdempotencyKey

        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api()  # no idem_key
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"

        r = api.post(
            url,
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert r.status_code == 400, r.data
        assert r.data["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        appt.refresh_from_db()
        assert appt.version == 1  # untouched
        assert appt.status == Appointment.Status.CONFIRMED
        assert not AppointmentRevision.objects.filter(appointment=appt).exists()
        assert not OutboxEvent.objects.filter(
            topic__in=[
                OutboxEvent.Topic.APPOINTMENT_RESCHEDULED,
                OutboxEvent.Topic.BOOKING_RESCHEDULED,
            ],
        ).exists()
        assert not IdempotencyKey.objects.filter(
            operation_name="booking.reschedule.internal",
        ).exists()

    def test_reschedule_with_empty_key_rejected_same_as_missing(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert r.status_code == 400, r.data
        assert r.data["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        appt.refresh_from_db()
        assert appt.version == 1

    def test_cancel_without_key_rejected_no_mutation(
        self, internal_customer, specialist, service,
    ):
        from appointments.models import IdempotencyKey

        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api()  # no idem_key
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/cancel/",
            {"reason": "changed my mind"}, format="json",
        )
        assert r.status_code == 400, r.data
        assert r.data["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED  # untouched
        assert not OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_CANCELLED,
        ).exists()
        assert not IdempotencyKey.objects.filter(
            operation_name="booking.cancel.internal",
        ).exists()

    def test_cancel_with_empty_key_rejected_same_as_missing(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/cancel/",
            {"reason": "changed my mind"}, format="json",
        )
        assert r.status_code == 400, r.data
        assert r.data["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_reschedule_with_key_works_and_dedups_normally(
        self, internal_customer, specialist, service,
    ):
        """With X-Idempotency-Key, both a first call and a same-body
        replay work — no version conflict, cached response returned
        verbatim on the replay."""
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="internal-policy-with-key-1")
        url = f"/api/v1/internal/appointments/{appt.id}/reschedule/"
        body = {
            "new_start_datetime": _grid_future_utc(72).isoformat(),
            "expected_version": 1,
        }

        first = api.post(url, body, format="json")
        second = api.post(url, body, format="json")
        assert first.status_code == 200, first.data
        assert second.status_code == 200
        assert first.data == second.data

    def test_cancel_with_key_works_normally(
        self, internal_customer, specialist, service,
    ):
        appt = _create_confirmed(internal_customer, specialist, service)
        api = _internal_api(idem_key="internal-policy-with-key-2")
        r = api.post(
            f"/api/v1/internal/appointments/{appt.id}/cancel/",
            {"reason": "changed my mind"}, format="json",
        )
        assert r.status_code == 200, r.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED


# ---------------------------------------------------------------------------
# 8. Mobile unversioned-reschedule compatibility gate (code review
#    2026-08-03) — settings.RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED.
# ---------------------------------------------------------------------------

class TestMobileUnversionedCompatibilityGate:
    def test_default_gate_open_two_unversioned_requests_lost_update(
        self, client_user, specialist, service,
    ):
        """Documents the CURRENT deliberate policy (gate default True):
        two mobile requests that both omit expected_version are NOT
        protected from each other — the second silently overwrites the
        first's result. This is the tracked, temporary legacy behaviour,
        not an oversight; see the setting's docstring for the removal
        condition."""
        appt = _create_confirmed(client_user, specialist, service)
        api = _mobile_api(client_user)
        url = f"/api/v1/appointments/{appt.id}/reschedule/"

        first_start = _grid_future_utc(72)
        second_start = _grid_future_utc(96)

        first = api.post(
            url, {"new_start_datetime": first_start.isoformat()}, format="json",
        )
        second = api.post(
            url, {"new_start_datetime": second_start.isoformat()}, format="json",
        )
        assert first.status_code == 200, first.data
        assert second.status_code == 200, second.data

        appt.refresh_from_db()
        assert appt.version == 3
        # The second (later) request won — first's intent was silently
        # discarded, the documented lost-update gap.
        assert appt.start_datetime == second_start

    def test_gate_closed_rejects_unversioned_request_no_mutation(
        self, settings, client_user, specialist, service,
    ):
        settings.RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED = False
        appt = _create_confirmed(client_user, specialist, service)
        api = _mobile_api(client_user)

        r = api.post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            {"new_start_datetime": _grid_future_utc(72).isoformat()},
            format="json",
        )
        assert r.status_code == 400, r.data
        assert r.data["error"]["code"] == "EXPECTED_VERSION_REQUIRED"

        appt.refresh_from_db()
        assert appt.version == 1  # untouched
        assert not AppointmentRevision.objects.filter(appointment=appt).exists()

    def test_gate_closed_still_allows_versioned_request(
        self, settings, client_user, specialist, service,
    ):
        settings.RESCHEDULE_MOBILE_UNVERSIONED_ALLOWED = False
        appt = _create_confirmed(client_user, specialist, service)
        api = _mobile_api(client_user)

        r = api.post(
            f"/api/v1/appointments/{appt.id}/reschedule/",
            {
                "new_start_datetime": _grid_future_utc(72).isoformat(),
                "expected_version": 1,
            },
            format="json",
        )
        assert r.status_code == 200, r.data
        appt.refresh_from_db()
        assert appt.version == 2
