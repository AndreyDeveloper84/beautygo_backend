"""DRF-1072 — the created time must belong to the actor's allowed context.

Three actor classes, three rule sets on the create path:

1. **Client booking** (``actor_role`` "user"/"client") — the full client
   contract: booking window, slot grid, schedule frame, salon closures,
   time-off. DRF-1062 added the frame + closure; this task adds the grid,
   the one piece of the read-path contract the write path still skipped.
2. **Staff booking** (walk-in, salon-recorded) — the schedule does not
   constrain: a client physically standing in front of the master at
   19:30 must be recordable even when the template ends at 19:00 and the
   client self-service window demands an hour's notice. Time-off still
   blocks — an absence is the master's own statement.
3. **Administrative override** — explicit, deliberate, audited: a trusted
   staff caller lifts even the absence, and the who/why is fixed in the
   existing audit mechanisms (log + the booking.created outbox payload).
   Never available to the client actor, never without a reason.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.exceptions import (
    BookingWindowError,
    SlotNotAvailableError,
)
from appointments.models import OutboxEvent, SpecialistTimeOff

MSK = ZoneInfo("Europe/Moscow")


def _next_weekday(weekday: int, weeks_ahead: int = 2) -> date:
    """A date far enough ahead that BOOKING_MIN_AHEAD_MINUTES never bites."""
    base = date.today() + timedelta(weeks=weeks_ahead)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def _utc(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=MSK).astimezone(
        timezone.utc
    )


def _grid_time_ahead(minutes: int) -> datetime:
    """A grid-aligned UTC start less than BOOKING_MIN_AHEAD_MINUTES away."""
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    t = t.replace(second=0, microsecond=0)
    return t.replace(minute=(t.minute // 30) * 30)


def _dto(client_user, specialist, service, start_at, actor_role, **kwargs):
    return CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=start_at,
        idempotency_key=str(uuid4()),
        payment_required=False,
        confirm_immediately=True,
        actor_role=actor_role,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Client contract — the slot grid
# ---------------------------------------------------------------------------

class TestClientGridAlignment:
    def test_client_off_grid_start_is_refused(
        self, client_user, specialist, service,
    ):
        """The read path only ever offers grid-aligned slots; a client
        POST that fabricates a 10:15 start must not land."""
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10, 15), actor_role="user",
        )

        with pytest.raises(BookingWindowError):
            CreateBookingService().execute(dto)

    def test_client_on_grid_start_is_accepted(
        self, client_user, specialist, service,
    ):
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10, 30), actor_role="user",
        )

        assert CreateBookingService().execute(dto).booking_id

    def test_staff_off_grid_start_is_allowed(
        self, client_user, specialist, service,
    ):
        """The grid is part of the client contract, not of the diary a
        master keeps for people who are physically present."""
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10, 15), actor_role="specialist",
        )

        assert CreateBookingService().execute(dto).booking_id


# ---------------------------------------------------------------------------
# 2. Staff context — the booking window does not constrain
# ---------------------------------------------------------------------------

class TestStaffBookingWindow:
    def test_staff_booking_inside_min_ahead_is_allowed(
        self, client_user, specialist, service,
    ):
        """The 19:30 case: the client stands in front of the master NOW.
        "At least 60 minutes ahead" is a self-service rule for clients
        planning their week, not a reason to refuse a human on the spot."""
        dto = _dto(
            client_user, specialist, service,
            _grid_time_ahead(10), actor_role="specialist",
        )

        assert CreateBookingService().execute(dto).booking_id

    def test_client_booking_inside_min_ahead_is_refused(
        self, client_user, specialist, service,
    ):
        dto = _dto(
            client_user, specialist, service,
            _grid_time_ahead(10), actor_role="user",
        )

        with pytest.raises(BookingWindowError):
            CreateBookingService().execute(dto)


# ---------------------------------------------------------------------------
# 3. Administrative override — explicit, reasoned, audited
# ---------------------------------------------------------------------------

class TestTimeOverride:
    def test_override_requires_a_reason(
        self, client_user, specialist, service,
    ):
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10), actor_role="specialist",
            time_override=True,
        )

        with pytest.raises(ValueError, match="reason"):
            CreateBookingService().execute(dto)

    def test_override_is_not_available_to_the_client(
        self, client_user, specialist, service,
    ):
        """A flag only a trusted caller may raise — the client actor
        passing it is a contract violation, not a booking."""
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10), actor_role="user",
            time_override=True, time_override_reason="сам себе админ",
        )

        with pytest.raises(ValueError, match="client"):
            CreateBookingService().execute(dto)

    def test_override_lifts_an_absence_and_is_audited(
        self, client_user, specialist_user, specialist, service, caplog,
    ):
        """The deliberate decision DRF-1062 deferred: staff without the
        flag stays blocked by the absence; the flagged, reasoned override
        lands and fixes who/why in the log and the outbox payload."""
        monday = _next_weekday(0)
        start = _utc(monday, 10)
        SpecialistTimeOff.objects.create(
            specialist=specialist,
            start_at=start,
            end_at=start + timedelta(hours=2),
            reason="личное",
        )

        dto = _dto(
            client_user, specialist, service, start,
            actor_role="specialist",
            time_override=True,
            time_override_reason="мастер подтвердил лично, отсутствие снято устно",
            actor_id=specialist_user.id,
        )
        with caplog.at_level(
            logging.WARNING,
            logger="appointments.application.services.create_booking_service",
        ):
            result = CreateBookingService().execute(dto)

        assert result.booking_id

        assert any(
            "booking.time_override" in rec.getMessage()
            and str(specialist_user.id) in rec.getMessage()
            for rec in caplog.records
        )

        event = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED)
        assert event.data["time_override"] is True
        assert event.data["time_override_reason"] == (
            "мастер подтвердил лично, отсутствие снято устно"
        )
        assert event.data["time_override_actor_id"] == str(specialist_user.id)

    def test_override_does_not_skip_the_conflict_check(
        self, client_user, specialist, service,
    ):
        """Override lifts the *time context* rules (window, grid, frame,
        closure, absence) — not physical reality: two bodies still cannot
        occupy the slot at once."""
        monday = _next_weekday(0)
        first = _dto(
            client_user, specialist, service,
            _utc(monday, 10), actor_role="specialist",
        )
        CreateBookingService().execute(first)

        clashing = _dto(
            client_user, specialist, service,
            _utc(monday, 10, 30), actor_role="specialist",
            time_override=True,
            time_override_reason="двойная запись по звонку",
        )
        with pytest.raises(SlotNotAvailableError):
            CreateBookingService().execute(clashing)

    def test_no_override_means_no_audit_fields(
        self, client_user, specialist, service,
    ):
        """The ordinary booking payload stays byte-identical: the audit
        keys appear only when an override actually happened."""
        dto = _dto(
            client_user, specialist, service,
            _utc(_next_weekday(0), 10), actor_role="specialist",
        )
        CreateBookingService().execute(dto)

        event = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CREATED)
        assert "time_override" not in event.data
