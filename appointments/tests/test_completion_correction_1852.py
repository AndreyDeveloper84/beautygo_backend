"""Service correction «состоялся → не пришёл» within a day (DRF-1852, OD-V2).

Owner decision OD-V2 (Linear DRF-1064, 2026-08-15): «разрешить исправление
в течение суток. Исполнение — через техподдержку и ручную отмену,
отдельного продуктового потока не строить»; the operator path must record
who corrected and why; the window lives in one configurable place.

What is locked:

* inside the window (23 h, and exactly at the edge) a completed visit
  becomes NO_SHOW, attributed, with both no-show events; the internal one
  carries the correction trace, the cross-service one is byte-identical to
  an ordinary no-show;
* 1 second past the window, without a reason, without an operator, twice,
  or on a visit that was never completed — refused, nothing written;
* the window is read from settings;
* the ordinary state machine is untouched: ``completed`` is still terminal
  and ``mark_no_show`` still refuses it (the new door is separate);
* the operator command: dry-run writes nothing, a real run corrects and
  warns about payments, a refusal is a ``CommandError`` with the code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.completion import (
    CorrectionRefused,
    correct_completion_to_no_show,
)
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import (
    BookingStateMachine,
    BookingStatus,
    TimeInterval,
)
from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

REASON = "клиент не пришёл, салон сообщил в поддержку"
OPERATOR = "support: Ivan"


@pytest.fixture(autouse=True)
def _window(settings):
    settings.BOOKING_COMPLETION_CORRECTION_WINDOW_HOURS = 24


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="salon-corr1852", name="Correction Salon")


@pytest.fixture
def specialist(db, salon):
    u = User.objects.create_user(
        username="corr1852_spec", password="x", role="specialist", phone="+79991852001",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Correction Master"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def service(specialist, db):
    category = ServiceCategory.objects.create(name="Corr Cat", slug="corr1852-cat")
    return Service.objects.create(
        specialist=specialist, category=category, name="Corr Service",
        price=Decimal("1000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="corr1852_client", password="x", role="client", phone="+79991852002",
    )


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=dt_timezone.utc)


def _booking(client_user, specialist, service, *, status, completed_at=None):
    """A booking built through the booking service, then moved into the past."""
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
        target_interval=TimeInterval(start_at=start, end_at=start + timedelta(hours=1)),
    )
    end = NOW - timedelta(days=2)
    Appointment.objects.filter(pk=appt.pk).update(
        status=status,
        start_datetime=end - timedelta(hours=1),
        end_datetime=end,
        completed_at=completed_at,
        completed_by="system" if completed_at else "",
    )
    OutboxEvent.objects.all().delete()
    appt.refresh_from_db()
    return appt


def _completed(client_user, specialist, service, *, ago: timedelta):
    return _booking(
        client_user, specialist, service,
        status=Appointment.Status.COMPLETED, completed_at=NOW - ago,
    )


def _topics():
    return sorted(e.topic for e in OutboxEvent.objects.all())


class TestInsideTheWindow:
    def test_23_hours_after_closing_becomes_no_show(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=23))
        closed_at = appt.completed_at

        correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW
        assert appt.no_show_marked_by == "salon"
        # The corrected fact stays on the row — the trace cites it.
        assert appt.completed_at == closed_at
        assert appt.completed_by == "system"

    def test_the_internal_event_carries_who_and_why(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=23))

        correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        internal = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_NO_SHOW)
        trace = internal.data["correction"]
        assert trace["from_status"] == "completed"
        assert trace["completed_by"] == "system"
        assert trace["reason"] == REASON
        assert trace["operator"] == OPERATOR
        assert trace["window_hours"] == 24
        assert trace["corrected_at"] == NOW.isoformat()

    def test_the_cross_service_event_is_an_ordinary_no_show(
        self, client_user, specialist, service
    ):
        """The bot's mirror must see exactly what an ordinary no-show emits —
        and nothing about the operator."""
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=1))

        correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        cancelled = OutboxEvent.objects.get(topic=OutboxEvent.Topic.BOOKING_CANCELLED)
        assert cancelled.data["reason_code"] == "user_no_show"
        assert cancelled.data["cancelled_by"] == "admin"
        assert "correction" not in cancelled.data
        assert OPERATOR not in str(cancelled.data)

    def test_exactly_at_the_edge_is_still_inside(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=24))

        correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW


class TestRefusals:
    def _assert_untouched(self, appt):
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert _topics() == []

    def test_one_second_past_the_window(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=24, seconds=1))

        with pytest.raises(CorrectionRefused) as exc:
            correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        assert exc.value.code == "window_closed"
        self._assert_untouched(appt)

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_without_a_reason(self, client_user, specialist, service, reason):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=1))

        with pytest.raises(CorrectionRefused) as exc:
            correct_completion_to_no_show(appt.id, reason=reason, operator=OPERATOR, now=NOW)

        assert exc.value.code == "reason_required"
        self._assert_untouched(appt)

    def test_without_an_operator(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=1))

        with pytest.raises(CorrectionRefused) as exc:
            correct_completion_to_no_show(appt.id, reason=REASON, operator=" ", now=NOW)

        assert exc.value.code == "operator_required"
        self._assert_untouched(appt)

    def test_twice(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(hours=1))
        correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        with pytest.raises(CorrectionRefused) as exc:
            correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        assert exc.value.code == "not_completed"
        assert _topics().count(OutboxEvent.Topic.BOOKING_NO_SHOW) == 1

    def test_a_visit_that_was_never_completed(self, client_user, specialist, service):
        appt = _booking(client_user, specialist, service, status=Appointment.Status.CONFIRMED)

        with pytest.raises(CorrectionRefused) as exc:
            correct_completion_to_no_show(appt.id, reason=REASON, operator=OPERATOR, now=NOW)

        assert exc.value.code == "not_completed"
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED


class TestTheWindowIsConfigurable:
    def test_a_two_hour_window(self, settings, client_user, specialist, service):
        settings.BOOKING_COMPLETION_CORRECTION_WINDOW_HOURS = 2
        late = _completed(client_user, specialist, service, ago=timedelta(hours=3))
        with pytest.raises(CorrectionRefused):
            correct_completion_to_no_show(late.id, reason=REASON, operator=OPERATOR, now=NOW)

        early = _completed(client_user, specialist, service, ago=timedelta(hours=1))
        correct_completion_to_no_show(early.id, reason=REASON, operator=OPERATOR, now=NOW)
        early.refresh_from_db()
        assert early.status == Appointment.Status.NO_SHOW


class TestTheOrdinaryFlowIsUntouched:
    def test_completed_is_still_terminal_for_ordinary_moves(self):
        assert BookingStateMachine.is_terminal(BookingStatus.COMPLETED) is True
        assert BookingStateMachine.can_transition(
            BookingStatus.COMPLETED, BookingStatus.NO_SHOW,
        ) is False

    def test_the_correction_door_is_exactly_one_move(self):
        assert BookingStateMachine.can_correct(BookingStatus.COMPLETED, BookingStatus.NO_SHOW)
        assert not BookingStateMachine.can_correct(BookingStatus.CANCELLED, BookingStatus.NO_SHOW)
        assert not BookingStateMachine.can_correct(BookingStatus.NO_SHOW, BookingStatus.COMPLETED)
        assert not BookingStateMachine.can_correct(BookingStatus.CONFIRMED, BookingStatus.NO_SHOW)

    def test_mark_no_show_still_refuses_a_completed_visit(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(minutes=1))
        with pytest.raises(ValidationError):
            appt.mark_no_show(marked_by="salon")


class TestOperatorCommand:
    def _run(self, *args):
        out = StringIO()
        call_command("correct_completion_to_no_show", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(minutes=5))
        Appointment.objects.filter(pk=appt.pk).update(
            completed_at=datetime.now(tz=dt_timezone.utc) - timedelta(minutes=5),
        )

        out = self._run(str(appt.id), "--reason", REASON, "--operator", OPERATOR, "--dry-run")

        assert "would correct to no_show" in out
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED
        assert _topics() == []

    def test_a_real_run_corrects_and_warns_about_payments(
        self, client_user, specialist, service
    ):
        appt = _completed(client_user, specialist, service, ago=timedelta(minutes=5))
        Appointment.objects.filter(pk=appt.pk).update(
            completed_at=datetime.now(tz=dt_timezone.utc) - timedelta(minutes=5),
        )

        out = self._run(str(appt.id), "--reason", REASON, "--operator", OPERATOR)

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.NO_SHOW
        assert "Payments are not touched" in out

    def test_a_refusal_is_a_command_error_with_the_code(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(minutes=5))
        Appointment.objects.filter(pk=appt.pk).update(
            completed_at=datetime.now(tz=dt_timezone.utc) - timedelta(hours=30),
        )

        with pytest.raises(CommandError, match="window_closed"):
            self._run(str(appt.id), "--reason", REASON, "--operator", OPERATOR)

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.COMPLETED

    def test_reason_is_required(self, client_user, specialist, service):
        appt = _completed(client_user, specialist, service, ago=timedelta(minutes=5))
        with pytest.raises(CommandError):
            self._run(str(appt.id), "--operator", OPERATOR)
