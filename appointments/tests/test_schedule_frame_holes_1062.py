"""DRF-1062 — per-date schedule overrides, salon closures, write-path guard.

Three things are proved here:

1. ``SpecialistScheduleException`` overrides the weekly template for one
   date — including the case ``SpecialistTimeOff`` structurally cannot
   express, opening a normally-closed day.
2. ``TenantClosure`` removes availability for the whole salon from one
   row, without any per-specialist fan-out.
3. The write path now refuses what the read path never offered. Before
   this task ``SpecialistWorkingHours`` was read by the slot query and by
   nothing else, so a direct booking for a non-working Sunday was
   accepted while slots for it were hidden.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from appointments.application.dto import GetAvailabilityDTO
from appointments.application.services._booking_guards import (
    check_schedule_frame,
    check_tenant_closure,
)
from appointments.application.services.availability_query_service import (
    AvailabilityQueryService,
)
from appointments.domain.exceptions import SlotNotAvailableError
from appointments.domain.value_objects import TimeInterval
from appointments.models import (
    SpecialistScheduleException,
    SpecialistWorkingHours,
    TenantClosure,
)
from tenants.models import Tenant

MSK = ZoneInfo("Europe/Moscow")


def _next_weekday(weekday: int, weeks_ahead: int = 2) -> date:
    """A date far enough ahead that BOOKING_MIN_AHEAD_MINUTES never bites."""
    base = date.today() + timedelta(weeks=weeks_ahead)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


def _utc(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=MSK).astimezone(
        timezone.utc
    )


def _interval(day: date, hh: int, minutes: int = 60) -> TimeInterval:
    start = _utc(day, hh)
    return TimeInterval(start_at=start, end_at=start + timedelta(minutes=minutes))


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="frame-holes-salon", name="Салон 1062")


@pytest.fixture
def scheduled_specialist(db, specialist, tenant):
    """Mon–Fri 10:00–19:00 with a 13:00–14:00 break. Sat/Sun closed."""
    specialist.tenant = tenant
    specialist.timezone = "Europe/Moscow"
    specialist.save(update_fields=["tenant", "timezone"])

    for day in range(7):
        working = day < 5
        SpecialistWorkingHours.objects.create(
            specialist=specialist,
            day_of_week=day,
            is_working_day=working,
            start_time=time(10, 0) if working else None,
            end_time=time(19, 0) if working else None,
            break_start=time(13, 0) if working else None,
            break_end=time(14, 0) if working else None,
        )
    return specialist


def _slots(specialist, service, day: date) -> list[str]:
    result = AvailabilityQueryService().get_day_availability(
        GetAvailabilityDTO(
            specialist_id=specialist.id, target_date=day, service_id=service.id,
        )
    )
    if not result.is_working_day:
        return []
    return [s.start_local for s in result.slots]


# ---------------------------------------------------------------------------
# Read path — the frame
# ---------------------------------------------------------------------------

class TestScheduleException:
    def test_weekly_template_is_the_baseline(self, scheduled_specialist, service):
        monday = _next_weekday(0)
        slots = _slots(scheduled_specialist, service, monday)

        assert slots, "Monday is a working day in the template"
        assert slots[0] == "10:00"
        # 60-min service, break 13:00-14:00 — nothing may start inside it,
        # and nothing may start at 12:30 either (it would run into 13:30).
        assert "13:00" not in slots
        assert "12:30" not in slots

    def test_sunday_is_closed_by_template(self, scheduled_specialist, service):
        assert _slots(scheduled_specialist, service, _next_weekday(6)) == []

    def test_exception_closes_a_working_day(self, scheduled_specialist, service):
        tuesday = _next_weekday(1)
        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist,
            date=tuesday,
            is_working_day=False,
            note="больничный",
        )

        assert _slots(scheduled_specialist, service, tuesday) == []

    def test_exception_narrows_a_working_day(self, scheduled_specialist, service):
        friday = _next_weekday(4)
        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist,
            date=friday,
            is_working_day=True,
            start_time=time(10, 0),
            end_time=time(15, 0),
            note="в эту пятницу до 15:00",
        )

        slots = _slots(scheduled_specialist, service, friday)

        assert slots[0] == "10:00"
        assert slots[-1] == "14:00", "a 60-min service must end by 15:00"
        assert "15:00" not in slots

    def test_exception_opens_a_closed_day(self, scheduled_specialist, service):
        """The case SpecialistTimeOff cannot express at all.

        A busy interval only subtracts; on a non-working weekday there is
        nothing to subtract from. Only a frame source can open the day.
        """
        sunday = _next_weekday(6)
        assert _slots(scheduled_specialist, service, sunday) == []

        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist,
            date=sunday,
            is_working_day=True,
            start_time=time(11, 0),
            end_time=time(16, 0),
            note="предпраздничное воскресенье",
        )

        slots = _slots(scheduled_specialist, service, sunday)

        assert slots[0] == "11:00"
        assert slots[-1] == "15:00"

    def test_exception_is_scoped_to_its_own_date(self, scheduled_specialist, service):
        wednesday = _next_weekday(2)
        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist,
            date=wednesday,
            is_working_day=False,
        )

        assert _slots(scheduled_specialist, service, wednesday) == []
        assert _slots(scheduled_specialist, service, wednesday + timedelta(days=1))

    def test_one_exception_per_specialist_and_date(self, scheduled_specialist):
        thursday = _next_weekday(3)
        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist, date=thursday, is_working_day=False,
        )

        with pytest.raises(Exception):
            SpecialistScheduleException.objects.create(
                specialist=scheduled_specialist, date=thursday, is_working_day=False,
            )


# ---------------------------------------------------------------------------
# Read path — the holes
# ---------------------------------------------------------------------------

class TestTenantClosure:
    def test_full_day_closure_removes_every_slot(
        self, scheduled_specialist, service, tenant,
    ):
        monday = _next_weekday(0)
        assert _slots(scheduled_specialist, service, monday)

        TenantClosure.objects.create(tenant=tenant, date=monday, reason="праздник")

        assert _slots(scheduled_specialist, service, monday) == []

    def test_partial_closure_removes_only_its_window(
        self, scheduled_specialist, service, tenant,
    ):
        monday = _next_weekday(0)
        TenantClosure.objects.create(
            tenant=tenant,
            date=monday,
            start_time=time(10, 0),
            end_time=time(15, 0),
            reason="дезинфекция",
        )

        slots = _slots(scheduled_specialist, service, monday)

        assert "10:00" not in slots
        assert "14:00" not in slots
        assert "15:00" in slots

    def test_closure_of_another_tenant_is_ignored(
        self, scheduled_specialist, service, db,
    ):
        other = Tenant.objects.create(slug="other-salon-1062", name="Другой салон")
        monday = _next_weekday(0)
        TenantClosure.objects.create(tenant=other, date=monday)

        assert _slots(scheduled_specialist, service, monday)

    def test_one_row_covers_every_specialist_of_the_tenant(
        self, scheduled_specialist, service, tenant, db,
    ):
        """No fan-out: the closure is stored once, not per specialist."""
        monday = _next_weekday(0)
        TenantClosure.objects.create(tenant=tenant, date=monday)

        from appointments.models import SpecialistTimeOff

        assert TenantClosure.objects.filter(tenant=tenant).count() == 1
        assert SpecialistTimeOff.objects.count() == 0
        assert _slots(scheduled_specialist, service, monday) == []


# ---------------------------------------------------------------------------
# Write path — the guard this task exists for
# ---------------------------------------------------------------------------

class TestWritePathHonoursSchedule:
    def test_booking_inside_working_hours_is_allowed(self, scheduled_specialist):
        check_schedule_frame(
            scheduled_specialist.id, _interval(_next_weekday(0), 11),
        )

    def test_booking_on_a_non_working_day_is_refused(self, scheduled_specialist):
        """The regression the whole task is about.

        Slots for Sunday were already hidden; a direct POST still landed.
        """
        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(
                scheduled_specialist.id, _interval(_next_weekday(6), 10),
            )

    def test_booking_before_opening_is_refused(self, scheduled_specialist):
        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(
                scheduled_specialist.id, _interval(_next_weekday(0), 8),
            )

    def test_booking_running_past_closing_is_refused(self, scheduled_specialist):
        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(
                scheduled_specialist.id, _interval(_next_weekday(0), 18, minutes=90),
            )

    def test_booking_over_the_break_is_refused(self, scheduled_specialist):
        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(
                scheduled_specialist.id, _interval(_next_weekday(0), 13),
            )

    def test_exception_governs_the_write_path_too(self, scheduled_specialist):
        """Read and write resolve the frame through the same function."""
        sunday = _next_weekday(6)
        SpecialistScheduleException.objects.create(
            specialist=scheduled_specialist,
            date=sunday,
            is_working_day=True,
            start_time=time(11, 0),
            end_time=time(16, 0),
        )

        check_schedule_frame(scheduled_specialist.id, _interval(sunday, 11))

        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(scheduled_specialist.id, _interval(sunday, 10))

    def test_closure_is_refused_on_the_write_path(self, scheduled_specialist, tenant):
        monday = _next_weekday(0)
        TenantClosure.objects.create(tenant=tenant, date=monday)

        with pytest.raises(SlotNotAvailableError):
            check_tenant_closure(scheduled_specialist.id, _interval(monday, 11))

    def test_specialist_without_a_tenant_has_no_closures(self, scheduled_specialist):
        scheduled_specialist.tenant = None
        scheduled_specialist.save(update_fields=["tenant"])

        check_tenant_closure(scheduled_specialist.id, _interval(_next_weekday(0), 11))


class TestEnforcementStartsWithADeclaredSchedule:
    """"No hours declared" is not "closed".

    A specialist who never published hours shows no slots either way, so
    nothing reaches the guard from the product. Refusing them outright
    would make an unknown number of live specialists unbookable for no
    gain. Enforcement is monotone — declaring a schedule only tightens it.
    """

    def test_specialist_without_any_hours_is_not_constrained(self, specialist):
        check_schedule_frame(specialist.id, _interval(_next_weekday(6), 3))

    def test_a_single_declared_day_turns_enforcement_on(self, specialist):
        SpecialistWorkingHours.objects.create(
            specialist=specialist,
            day_of_week=0,
            is_working_day=True,
            start_time=time(10, 0),
            end_time=time(19, 0),
        )

        check_schedule_frame(specialist.id, _interval(_next_weekday(0), 11))

        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(specialist.id, _interval(_next_weekday(0), 3))

        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(specialist.id, _interval(_next_weekday(6), 11))

    def test_an_exception_alone_turns_enforcement_on(self, specialist):
        """A per-date override counts as a declared schedule for that date."""
        sunday = _next_weekday(6)
        SpecialistScheduleException.objects.create(
            specialist=specialist,
            date=sunday,
            is_working_day=True,
            start_time=time(11, 0),
            end_time=time(16, 0),
        )

        check_schedule_frame(specialist.id, _interval(sunday, 11))

        with pytest.raises(SlotNotAvailableError):
            check_schedule_frame(specialist.id, _interval(sunday, 17))


class TestGuardAppliesToClientsNotStaff:
    """Who the schedule constrains.

    The schedule says when clients may book the salon. It does not
    overrule a master recording someone who is physically present: being
    told "нельзя" while the client stands in front of you is the exact
    failure this task exists to remove, only pointed at staff instead of
    at the sick master.
    """

    def _dto(self, client_user, specialist, service, start_at, actor_role):
        from uuid import uuid4

        from appointments.application.dto import CreateBookingDTO

        return CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=start_at,
            idempotency_key=str(uuid4()),
            payment_required=False,
            confirm_immediately=True,
            actor_role=actor_role,
        )

    def test_client_cannot_book_a_closed_sunday_through_the_service(
        self, client_user, scheduled_specialist, service,
    ):
        from appointments.application.services.create_booking_service import (
            CreateBookingService,
        )

        dto = self._dto(
            client_user, scheduled_specialist, service,
            _utc(_next_weekday(6), 10), actor_role="user",
        )

        with pytest.raises(SlotNotAvailableError):
            CreateBookingService().execute(dto)

    def test_walk_in_on_the_same_closed_sunday_is_allowed(
        self, client_user, scheduled_specialist, service,
    ):
        from appointments.application.services.create_booking_service import (
            CreateBookingService,
        )

        dto = self._dto(
            client_user, scheduled_specialist, service,
            _utc(_next_weekday(6), 10), actor_role="specialist",
        )

        result = CreateBookingService().execute(dto)

        assert result.booking_id
