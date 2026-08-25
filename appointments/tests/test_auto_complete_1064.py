"""DRF-1064 block B — visits that happened and that nobody closed.

The task itself is small; what needs pinning is everything around it.

* It must be **inert until deliberately switched on**, because
  ``booking.completed`` charges the platform fee and asks the client for
  a review. A registered beat entry that starts sweeping the moment it
  is deployed would bill a backlog.
* It must be **idempotent under a race**, and via the state machine
  rather than bookkeeping — a second worker on the same row produces one
  transition and one event.
* It must **attribute the closure to ``system``**, so a consumer can
  tell "the salon closed this" from "nobody did, so we did". Elapsed
  time is weak evidence (Ayla MVP Appointment Contract §5: "elapsed time
  alone is not completion evidence"); the field is what keeps that
  visible instead of laundering it into a human-looking closure.
* It must **not reach past the floor**, which is the whole point of the
  two-key ignition.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from appointments.tasks import auto_complete_elapsed_bookings
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="salon-autoc", name="Autoc Salon")


@pytest.fixture
def specialist(db, salon):
    u = User.objects.create_user(
        username="autoc_spec", password="x", role="specialist",
        phone="+79991020641",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Autoc Master"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def service(specialist, db):
    category = ServiceCategory.objects.create(
        name="Autoc Cat", slug="autoc-cat",
    )
    return Service.objects.create(
        specialist=specialist, category=category, name="Autoc Service",
        price=Decimal("1000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="autoc_client", password="x", role="client",
        phone="+79991020642",
    )


def _booking(client_user, specialist, service, *, ended_hours_ago: float):
    """A CONFIRMED booking that ended ``ended_hours_ago`` hours ago.

    Built through the booking service (so tenant, snapshots and the grant
    are real), then moved into the past directly — the engine refuses to
    create a booking in the past, which is exactly the state we need.
    """
    start = datetime.now(tz=dt_timezone.utc) + timedelta(hours=3)
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=start,
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=start, end_at=start + timedelta(hours=1),
        ),
    )
    end = timezone.now() - timedelta(hours=ended_hours_ago)
    Appointment.objects.filter(pk=appt.pk).update(
        status=Appointment.Status.CONFIRMED,
        start_datetime=end - timedelta(hours=1),
        end_datetime=end,
    )
    OutboxEvent.objects.all().delete()
    appt.refresh_from_db()
    return appt


def _enabled(settings, *, floor_days_ago: int = 30, hours: int = 3):
    settings.BOOKING_AUTO_COMPLETE_ENABLED = True
    settings.BOOKING_AUTO_COMPLETE_AFTER_HOURS = hours
    settings.BOOKING_AUTO_COMPLETE_NOT_BEFORE = (
        timezone.now() - timedelta(days=floor_days_ago)
    ).isoformat()
    settings.BOOKING_AUTO_COMPLETE_BATCH_SIZE = 200


@pytest.mark.django_db(transaction=True)
class TestIgnition:

    def test_disabled_by_default_touches_nothing(
        self, settings, client_user, specialist, service,
    ):
        settings.BOOKING_AUTO_COMPLETE_ENABLED = False
        appt = _booking(client_user, specialist, service, ended_hours_ago=9)

        result = auto_complete_elapsed_bookings()

        assert result["ran"] is False
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.exists()

    def test_enabled_without_a_floor_refuses_to_sweep(
        self, settings, client_user, specialist, service,
    ):
        """The case this guard exists for.

        Switching the feature on with no floor and a backlog present
        would bill and message the entire backlog on the first tick. The
        task declines and says so rather than doing the plausible thing.
        """
        settings.BOOKING_AUTO_COMPLETE_ENABLED = True
        settings.BOOKING_AUTO_COMPLETE_NOT_BEFORE = ""
        appt = _booking(client_user, specialist, service, ended_hours_ago=9)

        result = auto_complete_elapsed_bookings()

        assert result["ran"] is False
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_unparseable_floor_also_refuses(
        self, settings, client_user, specialist, service,
    ):
        settings.BOOKING_AUTO_COMPLETE_ENABLED = True
        settings.BOOKING_AUTO_COMPLETE_NOT_BEFORE = "soon"
        appt = _booking(client_user, specialist, service, ended_hours_ago=9)

        assert auto_complete_elapsed_bookings()["ran"] is False
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
class TestSweep:

    def test_closes_a_visit_that_ended_long_enough_ago(
        self, settings, client_user, specialist, service,
    ):
        _enabled(settings)
        appt = _booking(client_user, specialist, service, ended_hours_ago=4)

        result = auto_complete_elapsed_bookings()

        assert result["completed"] == 1
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert appt.completed_at is not None
        assert appt.completed_by == "system"

    def test_event_says_system_and_stays_at_version_1(
        self, settings, client_user, specialist, service,
    ):
        _enabled(settings)
        appt = _booking(client_user, specialist, service, ended_hours_ago=4)

        auto_complete_elapsed_bookings()

        evt = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        )
        assert evt.payload["event_version"] == 1
        assert evt.payload["data"]["completed_by"] == "system"
        assert evt.payload["actor"] == "system"
        assert evt.payload["user_id"] == str(appt.client_id)
        # One timestamp, not two calls to now(): the payload reads the
        # value stored on the row.
        appt.refresh_from_db()
        assert evt.payload["data"]["completed_at"] == \
            appt.completed_at.isoformat()

    def test_leaves_a_visit_still_inside_the_grace_period(
        self, settings, client_user, specialist, service,
    ):
        _enabled(settings, hours=3)
        appt = _booking(client_user, specialist, service, ended_hours_ago=1)

        assert auto_complete_elapsed_bookings()["completed"] == 0
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_does_not_reach_past_the_floor(
        self, settings, client_user, specialist, service,
    ):
        """The backlog stays where it is until someone drains it by hand."""
        _enabled(settings, floor_days_ago=2)
        old = _booking(client_user, specialist, service, ended_hours_ago=24 * 9)

        assert auto_complete_elapsed_bookings()["completed"] == 0
        old.refresh_from_db()
        assert old.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.exists()

    def test_ignores_bookings_that_are_not_confirmed(
        self, settings, client_user, specialist, service,
    ):
        _enabled(settings)
        appt = _booking(client_user, specialist, service, ended_hours_ago=6)
        Appointment.objects.filter(pk=appt.pk).update(
            status=Appointment.Status.CANCELLED,
        )

        assert auto_complete_elapsed_bookings()["completed"] == 0
        assert not OutboxEvent.objects.exists()

    def test_second_run_is_a_no_op(
        self, settings, client_user, specialist, service,
    ):
        """Idempotency comes from the state machine: the second pass
        finds nothing CONFIRMED and emits no second event."""
        _enabled(settings)
        _booking(client_user, specialist, service, ended_hours_ago=5)

        first = auto_complete_elapsed_bookings()
        second = auto_complete_elapsed_bookings()

        assert first["completed"] == 1
        assert second["completed"] == 0
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
        ).count() == 1

    def test_batch_size_caps_the_work(
        self, settings, client_user, specialist, service,
    ):
        _enabled(settings)
        settings.BOOKING_AUTO_COMPLETE_BATCH_SIZE = 1
        _booking(client_user, specialist, service, ended_hours_ago=6)
        _booking(client_user, specialist, service, ended_hours_ago=5)

        assert auto_complete_elapsed_bookings()["completed"] == 1
        assert Appointment.objects.filter(
            status=Appointment.Status.CONFIRMED,
        ).count() == 1


@pytest.mark.django_db(transaction=True)
class TestBacklogCommand:
    """The other half of the two-key ignition: the tail has a path.

    A floor that nothing can ever reach past would just be a way of
    losing the backlog quietly. This command is how it gets drained —
    with a window, a cap and a dry run, by someone who knows the closure
    bills and messages the client.
    """

    def test_dry_run_writes_nothing(
        self, client_user, specialist, service,
    ):
        appt = _booking(client_user, specialist, service, ended_hours_ago=24 * 9)
        out = StringIO()

        call_command(
            "complete_elapsed_backlog",
            "--since", (timezone.now() - timedelta(days=30)).date().isoformat(),
            "--dry-run", stdout=out,
        )

        assert "would close" in out.getvalue()
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
        assert not OutboxEvent.objects.exists()

    def test_closes_the_backlog_when_asked(
        self, client_user, specialist, service,
    ):
        appt = _booking(client_user, specialist, service, ended_hours_ago=24 * 9)
        out = StringIO()

        call_command(
            "complete_elapsed_backlog",
            "--since", (timezone.now() - timedelta(days=30)).date().isoformat(),
            stdout=out,
        )

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        # Attributed to `system` exactly like the sweep — the backlog is
        # not a different kind of closure, only a differently triggered
        # one.
        assert appt.completed_by == "system"

    def test_refuses_a_window_where_nothing_has_elapsed(self, db):
        with pytest.raises(CommandError):
            call_command(
                "complete_elapsed_backlog",
                "--since", (timezone.now() + timedelta(days=1)).date().isoformat(),
            )

    def test_limit_caps_the_run(
        self, client_user, specialist, service,
    ):
        _booking(client_user, specialist, service, ended_hours_ago=24 * 9)
        _booking(client_user, specialist, service, ended_hours_ago=24 * 8)
        out = StringIO()

        call_command(
            "complete_elapsed_backlog",
            "--since", (timezone.now() - timedelta(days=30)).date().isoformat(),
            "--limit", "1", stdout=out,
        )

        assert Appointment.objects.filter(
            status=Appointment.Status.COMPLETED,
        ).count() == 1
        assert "re-run to continue" in out.getvalue()


# ---------------------------------------------------------------------------
# DRF-1048 — a pass that did nothing must be distinguishable from a pass
# that never happened.
# ---------------------------------------------------------------------------

def _pass_lines(caplog):
    """Every ``booking.auto_complete.pass`` line the run emitted."""
    return [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("booking.auto_complete.pass")
    ]


def _one_pass_line(caplog):
    lines = _pass_lines(caplog)
    assert len(lines) == 1, f"expected exactly one pass line, got {lines}"
    return lines[0]


@pytest.mark.django_db(transaction=True)
class TestEveryPassLeavesATrace:
    """The reason DRF-1048 was open for weeks: the sweep was registered in
    beat, ran every 15 minutes, and wrote nothing to the log on any of
    those ticks — neither when it was gated off, nor when it ran and
    matched nothing. "No log lines" was therefore consistent with *three*
    different failures at once, and told an operator which one it was: no
    idea. Every exit path below now names itself.
    """

    def test_gated_off_still_says_so(
        self, settings, caplog, client_user, specialist, service,
    ):
        settings.BOOKING_AUTO_COMPLETE_ENABLED = False
        _booking(client_user, specialist, service, ended_hours_ago=9)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            result = auto_complete_elapsed_bookings()

        assert result["ran"] is False
        assert result["reason"] == "disabled"
        line = _one_pass_line(caplog)
        assert "ran=false" in line
        assert "reason=disabled" in line
        # Names the knob, so the log line is actionable without the source.
        assert "BOOKING_AUTO_COMPLETE_ENABLED" in line

    def test_missing_floor_says_which_key_is_missing(
        self, settings, caplog, client_user, specialist, service,
    ):
        settings.BOOKING_AUTO_COMPLETE_ENABLED = True
        settings.BOOKING_AUTO_COMPLETE_NOT_BEFORE = ""
        _booking(client_user, specialist, service, ended_hours_ago=9)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            result = auto_complete_elapsed_bookings()

        assert result["ran"] is False
        assert result["reason"] == "no_floor"
        assert "reason=no_floor" in _one_pass_line(caplog)

    def test_a_pass_that_found_nothing_is_a_log_line_not_silence(
        self, settings, caplog, client_user, specialist, service,
    ):
        """The empty pass — the case that used to be indistinguishable
        from the task not running at all."""
        _enabled(settings)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            result = auto_complete_elapsed_bookings()

        assert result["ran"] is True
        assert result["candidates"] == 0
        line = _one_pass_line(caplog)
        assert "ran=true" in line
        assert "candidates=0" in line
        assert "completed=0" in line

    def test_empty_pass_names_the_bookings_it_was_not_allowed_to_touch(
        self, settings, caplog, client_user, specialist, service,
    ):
        """An empty pass has two very different meanings: nothing
        happened, or plenty happened and none of it was eligible. The
        line separates them so the next question is answerable from the
        log alone.
        """
        _enabled(settings, floor_days_ago=2)
        # Elapsed, inside the window, but never reached CONFIRMED — the
        # sweep must not touch it, and an operator must be able to see
        # that this is why the pass was empty.
        stuck = _booking(client_user, specialist, service, ended_hours_ago=9)
        Appointment.objects.filter(pk=stuck.pk).update(
            status=Appointment.Status.AWAITING_PAYMENT,
        )
        # Elapsed and CONFIRMED, but older than the floor — the backlog
        # the manual command exists for.
        _booking(client_user, specialist, service, ended_hours_ago=24 * 9)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            result = auto_complete_elapsed_bookings()

        assert result["candidates"] == 0
        line = _one_pass_line(caplog)
        assert "elapsed_unconfirmed=1" in line
        assert "below_floor=1" in line

    def test_a_pass_that_worked_reports_its_counts(
        self, settings, caplog, client_user, specialist, service,
    ):
        _enabled(settings)
        _booking(client_user, specialist, service, ended_hours_ago=4)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            result = auto_complete_elapsed_bookings()

        assert result["completed"] == 1
        line = _one_pass_line(caplog)
        assert "candidates=1" in line
        assert "completed=1" in line
        assert "skipped=0" in line
        assert "failed=0" in line

    def test_the_line_carries_the_window_it_used(
        self, settings, caplog, client_user, specialist, service,
    ):
        """Wrong-window bugs (timezone, grace period, floor) are invisible
        unless the pass says which window it actually swept."""
        _enabled(settings, hours=3)

        with caplog.at_level(logging.INFO, logger="appointments.tasks"):
            auto_complete_elapsed_bookings()

        line = _one_pass_line(caplog)
        assert "cutoff=" in line
        assert "not_before=" in line
