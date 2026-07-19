"""Integration tests for application services — requires DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.utils import timezone as dj_tz

from appointments.application.dto import (
    CancelBookingDTO,
    CreateBookingDTO,
    GetAvailabilityDTO,
    RescheduleBookingDTO,
)
from appointments.application.services.availability_query_service import (
    AvailabilityQueryService,
)
from appointments.application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.exceptions import (
    BookingWindowError,
    CancellationNotAllowedError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
    SpecialistNotActiveError,
)
from appointments.models import (
    Appointment,
    OutboxEvent,
    SpecialistWorkingHours,
)
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='svc_specialist', password='pass', role='specialist',
        phone='+79990001100',
    )


@pytest.fixture
def specialist(specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = 'Service Test Specialist'
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = 'Europe/Moscow'
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='SvcTest Cat', slug='svctest-cat')


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name='Test Haircut',
        price='2000.00',
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='svc_client', password='pass', role='client',
        phone='+79990001101',
    )


def _future_utc(hours: int = 3) -> datetime:
    """Returns a datetime `hours` from now in UTC, rounded to the minute."""
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------
# CreateBookingService
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCreateBookingService:
    def test_happy_path(self, client_user, specialist, service):
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=str(uuid4()),
        )
        result = CreateBookingService().execute(dto)

        assert result.status == "awaiting_payment"
        assert result.service_name == "Test Haircut"
        assert Appointment.objects.count() == 1
        assert Payment.objects.count() == 1
        assert OutboxEvent.objects.filter(topic="booking.created").count() == 1

        appt = Appointment.objects.first()
        assert appt.snapshot_service_name == "Test Haircut"
        # Flat 90₽ fee (D1) on a 2000₽ service → effective rate 4.50.
        assert appt.snapshot_platform_fee == Decimal("90.00")
        assert appt.snapshot_specialist_income == Decimal("1910.00")
        assert appt.snapshot_commission_percent == Decimal("4.50")

    def test_idempotency_same_key_returns_same_booking(
        self, client_user, specialist, service,
    ):
        key = str(uuid4())
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=key,
        )
        result1 = CreateBookingService().execute(dto)
        result2 = CreateBookingService().execute(dto)

        assert result1.booking_id == result2.booking_id
        assert Appointment.objects.count() == 1

    def test_slot_conflict(self, client_user, specialist, service):
        start = _future_utc(3)
        dto1 = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=start,
            idempotency_key=str(uuid4()),
        )
        CreateBookingService().execute(dto1)

        dto2 = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=start + timedelta(minutes=30),  # overlaps with 60-min service
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(SlotNotAvailableError):
            CreateBookingService().execute(dto2)

    def test_adjacent_slots_succeed(self, client_user, specialist, service):
        """Back-to-back bookings should work (touching, not overlapping)."""
        start1 = _future_utc(3)
        start2 = start1 + timedelta(minutes=60)  # exactly after first

        CreateBookingService().execute(CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=start1,
            idempotency_key=str(uuid4()),
        ))
        result2 = CreateBookingService().execute(CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=start2,
            idempotency_key=str(uuid4()),
        ))
        assert result2.status == "awaiting_payment"
        assert Appointment.objects.count() == 2

    def test_specialist_not_active(self, client_user, specialist, service):
        specialist.is_booking_enabled = False
        specialist.save(update_fields=['is_booking_enabled'])

        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(SpecialistNotActiveError):
            CreateBookingService().execute(dto)

    def test_booking_too_soon(self, client_user, specialist, service):
        soon = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=soon,
            idempotency_key=str(uuid4()),
        )
        with pytest.raises(BookingWindowError):
            CreateBookingService().execute(dto)


# ---------------------------------------------------------------------------
# CancelBookingService
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCancelBookingService:
    def _create_confirmed_appointment(self, client_user, specialist, service):
        start = _future_utc(48)
        appt = Appointment.objects.create(
            client=client_user,
            specialist=specialist,
            service=service,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=60),
            price=service.price,
            status=Appointment.Status.CONFIRMED,
        )
        return appt

    def test_client_cancels_successfully(self, client_user, specialist, service):
        appt = self._create_confirmed_appointment(client_user, specialist, service)
        dto = CancelBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            initiator_role="client",
            reason="Changed my mind",
        )
        CancelBookingService().execute(dto)

        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        assert appt.cancellation_reason == "Changed my mind"
        assert OutboxEvent.objects.filter(topic="booking.cancelled").count() == 1

    def test_cannot_cancel_completed(self, client_user, specialist, service):
        appt = self._create_confirmed_appointment(client_user, specialist, service)
        appt.status = Appointment.Status.COMPLETED
        appt.save(update_fields=['status'])

        dto = CancelBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            initiator_role="client",
        )
        with pytest.raises(CancellationNotAllowedError):
            CancelBookingService().execute(dto)


# ---------------------------------------------------------------------------
# RescheduleBookingService
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestRescheduleBookingService:
    def _create_confirmed_appointment(self, client_user, specialist, service):
        start = _future_utc(48)
        return Appointment.objects.create(
            client=client_user,
            specialist=specialist,
            service=service,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=60),
            price=service.price,
            status=Appointment.Status.CONFIRMED,
        )

    def test_reschedule_success(self, client_user, specialist, service):
        appt = self._create_confirmed_appointment(client_user, specialist, service)
        new_start = _future_utc(72)

        dto = RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        )
        RescheduleBookingService().execute(dto)

        appt.refresh_from_db()
        assert appt.start_datetime == new_start
        assert OutboxEvent.objects.filter(topic="booking.rescheduled").count() == 1

    def test_reschedule_emits_event_with_start_at_for_cache_invalidation(
        self, client_user, specialist, service,
    ):
        """Regression for Phase A.5 (REFACTOR_PRIORITIZATION) — payload uses
        the same ``start_at`` field name as booking.created so the outbox
        handler ``_invalidate_slots_from_payload`` finds the new date and
        invalidates cache. Without this, new-date slots stayed stale after
        a reschedule because the handler was reading ``start_at`` while the
        old payload used ``new_start_at``.
        """
        appt = self._create_confirmed_appointment(client_user, specialist, service)
        new_start = _future_utc(72)

        dto = RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=new_start,
        )
        RescheduleBookingService().execute(dto)

        event = OutboxEvent.objects.get(topic="booking.rescheduled")
        # Post-#486 the payload is an ADR-0009 envelope; domain fields
        # live under .data (or via the OutboxEvent.data convenience
        # property). Cache-invalidation handler reads via that path too.
        assert "start_at" in event.data, "missing start_at — cache won't invalidate"
        assert "end_at" in event.data
        assert "old_start_at" in event.data
        assert event.data["start_at"] == new_start.isoformat()

    def test_cannot_reschedule_pending(self, client_user, specialist, service):
        start = _future_utc(48)
        appt = Appointment.objects.create(
            client=client_user,
            specialist=specialist,
            service=service,
            start_datetime=start,
            end_datetime=start + timedelta(minutes=60),
            price=service.price,
            status=Appointment.Status.PENDING,
        )
        dto = RescheduleBookingDTO(
            booking_id=appt.id,
            initiator_user_id=client_user.id,
            new_start_at=_future_utc(72),
        )
        with pytest.raises(RescheduleNotAllowedError):
            RescheduleBookingService().execute(dto)

    def test_reschedule_conflict(self, client_user, specialist, service):
        """Cannot reschedule to a slot that is already taken."""
        appt1 = self._create_confirmed_appointment(client_user, specialist, service)

        # Create another booking at a different time
        other_start = _future_utc(72)
        Appointment.objects.create(
            client=client_user,
            specialist=specialist,
            service=service,
            start_datetime=other_start,
            end_datetime=other_start + timedelta(minutes=60),
            price=service.price,
            status=Appointment.Status.CONFIRMED,
        )

        dto = RescheduleBookingDTO(
            booking_id=appt1.id,
            initiator_user_id=client_user.id,
            new_start_at=other_start + timedelta(minutes=30),  # overlaps
        )
        with pytest.raises(SlotNotAvailableError):
            RescheduleBookingService().execute(dto)


# ---------------------------------------------------------------------------
# AvailabilityQueryService
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAvailabilityQueryService:
    def test_no_working_hours_returns_not_working(self, specialist, service):
        """If no SpecialistWorkingHours set, day is not a working day."""
        tomorrow = (dj_tz.localdate() + timedelta(days=1))
        dto = GetAvailabilityDTO(
            specialist_id=specialist.id,
            target_date=tomorrow,
            service_id=service.id,
        )
        result = AvailabilityQueryService().get_day_availability(dto)
        assert result.is_working_day is False
        assert result.slots == []

    def test_with_working_hours_returns_slots(self, specialist, service):
        tomorrow = dj_tz.localdate() + timedelta(days=1)
        day_of_week = tomorrow.weekday()

        SpecialistWorkingHours.objects.create(
            specialist=specialist,
            day_of_week=day_of_week,
            is_working_day=True,
            start_time="09:00",
            end_time="18:00",
        )

        dto = GetAvailabilityDTO(
            specialist_id=specialist.id,
            target_date=tomorrow,
            service_id=service.id,
        )
        result = AvailabilityQueryService().get_day_availability(dto)
        assert result.is_working_day is True
        assert len(result.slots) > 0
        # 60-min service in 9h window (09:00-18:00) = up to 18 slots (every 30 min)
        assert len(result.slots) <= 18

    def test_booked_slot_excluded(self, client_user, specialist, service):
        tomorrow = dj_tz.localdate() + timedelta(days=1)
        day_of_week = tomorrow.weekday()

        SpecialistWorkingHours.objects.create(
            specialist=specialist,
            day_of_week=day_of_week,
            is_working_day=True,
            start_time="09:00",
            end_time="18:00",
        )

        # Get slots before booking
        dto = GetAvailabilityDTO(
            specialist_id=specialist.id,
            target_date=tomorrow,
            service_id=service.id,
        )
        before = AvailabilityQueryService().get_day_availability(dto)

        # Book one slot (10:00 Moscow time)
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Moscow")
        slot_start = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day,
            10, 0, tzinfo=tz,
        ).astimezone(timezone.utc)
        slot_end = slot_start + timedelta(minutes=60)

        Appointment.objects.create(
            client=client_user,
            specialist=specialist,
            service=service,
            start_datetime=slot_start,
            end_datetime=slot_end,
            price=service.price,
            status=Appointment.Status.CONFIRMED,
        )

        # Invalidate cache and get slots after booking
        from appointments.infrastructure.cache.slot_cache import SlotCacheService
        SlotCacheService().invalidate(specialist.id, tomorrow)

        after = AvailabilityQueryService().get_day_availability(dto)
        assert len(after.slots) < len(before.slots)


# ---------------------------------------------------------------------------
# S3-CAL recheck-at-confirm (external busy) — Level-1, behind EXTERNAL_BUSY_ENABLED
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestExternalBusyRecheckAtConfirm:
    def test_flag_on_external_busy_blocks_booking(
        self, settings, client_user, specialist, service,
    ):
        settings.EXTERNAL_BUSY_ENABLED = True
        from appointments.domain.exceptions import ExternalSlotTakenError
        from services.models import ExternalBusyInterval

        start = _future_utc(4)
        ExternalBusyInterval.objects.create(
            tenant=specialist.tenant, specialist=specialist,
            start_at=start, end_at=start + timedelta(minutes=60),
            external_id="ext-busy-block",
        )
        dto = CreateBookingDTO(
            client_id=client_user.id, specialist_id=specialist.id,
            service_id=service.id, start_at=start, idempotency_key=str(uuid4()),
        )
        with pytest.raises(ExternalSlotTakenError):
            CreateBookingService().execute(dto)
        assert Appointment.objects.count() == 0

    def test_flag_off_external_busy_ignored(
        self, settings, client_user, specialist, service,
    ):
        settings.EXTERNAL_BUSY_ENABLED = False
        from services.models import ExternalBusyInterval

        start = _future_utc(5)
        ExternalBusyInterval.objects.create(
            tenant=specialist.tenant, specialist=specialist,
            start_at=start, end_at=start + timedelta(minutes=60),
            external_id="ext-busy-ignored",
        )
        dto = CreateBookingDTO(
            client_id=client_user.id, specialist_id=specialist.id,
            service_id=service.id, start_at=start, idempotency_key=str(uuid4()),
        )
        result = CreateBookingService().execute(dto)
        assert result.booking_id is not None
