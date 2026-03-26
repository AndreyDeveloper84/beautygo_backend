"""Unit tests for domain layer — pure Python, no DB required."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from appointments.domain.exceptions import (
    BookingWindowError,
    CancellationNotAllowedError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
)
from appointments.domain.policies import (
    DefaultBookingWindowPolicy,
    DefaultCommissionPolicy,
    StandardCancellationPolicy,
    StandardReschedulePolicy,
)
from appointments.domain.value_objects import (
    ACTIVE_BOOKING_STATUSES,
    BookingSnapshot,
    BookingStateMachine,
    BookingStatus,
    TimeInterval,
)


# ---------------------------------------------------------------------------
# TimeInterval
# ---------------------------------------------------------------------------

class TestTimeInterval:
    def _utc(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 3, 26, hour, minute, tzinfo=timezone.utc)

    def test_valid_creation(self):
        ti = TimeInterval(start_at=self._utc(10), end_at=self._utc(11))
        assert ti.duration_minutes() == 60

    def test_start_after_end_raises(self):
        with pytest.raises(ValueError, match="start_at must be before end_at"):
            TimeInterval(start_at=self._utc(12), end_at=self._utc(10))

    def test_equal_start_end_raises(self):
        with pytest.raises(ValueError):
            TimeInterval(start_at=self._utc(10), end_at=self._utc(10))

    def test_naive_datetime_raises(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            TimeInterval(
                start_at=datetime(2026, 3, 26, 10, 0),
                end_at=datetime(2026, 3, 26, 11, 0),
            )

    def test_overlaps_true(self):
        a = TimeInterval(self._utc(10), self._utc(12))
        b = TimeInterval(self._utc(11), self._utc(13))
        assert a.overlaps(b) is True
        assert b.overlaps(a) is True

    def test_overlaps_contained(self):
        outer = TimeInterval(self._utc(10), self._utc(14))
        inner = TimeInterval(self._utc(11), self._utc(13))
        assert outer.overlaps(inner) is True

    def test_touching_does_not_overlap(self):
        """Back-to-back appointments should be allowed."""
        a = TimeInterval(self._utc(10), self._utc(11))
        b = TimeInterval(self._utc(11), self._utc(12))
        assert a.overlaps(b) is False
        assert b.overlaps(a) is False

    def test_no_overlap_disjoint(self):
        a = TimeInterval(self._utc(10), self._utc(11))
        b = TimeInterval(self._utc(13), self._utc(14))
        assert a.overlaps(b) is False

    def test_duration_minutes(self):
        ti = TimeInterval(self._utc(9, 30), self._utc(11, 0))
        assert ti.duration_minutes() == 90


# ---------------------------------------------------------------------------
# BookingStateMachine
# ---------------------------------------------------------------------------

class TestBookingStateMachine:
    def test_pending_to_awaiting_payment(self):
        result = BookingStateMachine.transition(
            BookingStatus.PENDING, BookingStatus.AWAITING_PAYMENT,
        )
        assert result == BookingStatus.AWAITING_PAYMENT

    def test_pending_to_cancelled(self):
        result = BookingStateMachine.transition(
            BookingStatus.PENDING, BookingStatus.CANCELLED,
        )
        assert result == BookingStatus.CANCELLED

    def test_awaiting_payment_to_confirmed(self):
        result = BookingStateMachine.transition(
            BookingStatus.AWAITING_PAYMENT, BookingStatus.CONFIRMED,
        )
        assert result == BookingStatus.CONFIRMED

    def test_confirmed_to_completed(self):
        result = BookingStateMachine.transition(
            BookingStatus.CONFIRMED, BookingStatus.COMPLETED,
        )
        assert result == BookingStatus.COMPLETED

    def test_confirmed_to_no_show(self):
        result = BookingStateMachine.transition(
            BookingStatus.CONFIRMED, BookingStatus.NO_SHOW,
        )
        assert result == BookingStatus.NO_SHOW

    def test_cannot_skip_states(self):
        """PENDING -> COMPLETED is not allowed (must go through AWAITING_PAYMENT)."""
        with pytest.raises(InvalidStateTransitionError):
            BookingStateMachine.transition(
                BookingStatus.PENDING, BookingStatus.COMPLETED,
            )

    def test_cannot_cancel_completed(self):
        with pytest.raises(InvalidStateTransitionError):
            BookingStateMachine.transition(
                BookingStatus.COMPLETED, BookingStatus.CANCELLED,
            )

    def test_cannot_transition_from_cancelled(self):
        with pytest.raises(InvalidStateTransitionError):
            BookingStateMachine.transition(
                BookingStatus.CANCELLED, BookingStatus.CONFIRMED,
            )

    def test_is_terminal(self):
        assert BookingStateMachine.is_terminal(BookingStatus.COMPLETED) is True
        assert BookingStateMachine.is_terminal(BookingStatus.CANCELLED) is True
        assert BookingStateMachine.is_terminal(BookingStatus.NO_SHOW) is True
        assert BookingStateMachine.is_terminal(BookingStatus.PENDING) is False
        assert BookingStateMachine.is_terminal(BookingStatus.CONFIRMED) is False

    def test_can_transition(self):
        assert BookingStateMachine.can_transition(
            BookingStatus.CONFIRMED, BookingStatus.COMPLETED,
        ) is True
        assert BookingStateMachine.can_transition(
            BookingStatus.COMPLETED, BookingStatus.CANCELLED,
        ) is False

    def test_active_statuses(self):
        assert BookingStatus.PENDING in ACTIVE_BOOKING_STATUSES
        assert BookingStatus.AWAITING_PAYMENT in ACTIVE_BOOKING_STATUSES
        assert BookingStatus.CONFIRMED in ACTIVE_BOOKING_STATUSES
        assert BookingStatus.COMPLETED not in ACTIVE_BOOKING_STATUSES
        assert BookingStatus.CANCELLED not in ACTIVE_BOOKING_STATUSES


# ---------------------------------------------------------------------------
# BookingSnapshot
# ---------------------------------------------------------------------------

class TestBookingSnapshot:
    def test_create_calculates_fees(self):
        snap = BookingSnapshot.create(
            service_name="Маникюр",
            duration_minutes=60,
            price=Decimal("2000.00"),
            commission_percent=Decimal("8.0"),
            specialist_timezone="Europe/Moscow",
            buffer_after_minutes=10,
        )
        assert snap.platform_fee == Decimal("160.00")
        assert snap.specialist_income == Decimal("1840.00")
        assert snap.service_name == "Маникюр"
        assert snap.buffer_after_minutes == 10

    def test_zero_commission(self):
        snap = BookingSnapshot.create(
            service_name="Test",
            duration_minutes=30,
            price=Decimal("1000.00"),
            commission_percent=Decimal("0"),
            specialist_timezone="UTC",
        )
        assert snap.platform_fee == Decimal("0.00")
        assert snap.specialist_income == Decimal("1000.00")

    def test_snapshot_is_immutable(self):
        snap = BookingSnapshot.create(
            service_name="Test",
            duration_minutes=30,
            price=Decimal("500.00"),
            commission_percent=Decimal("10"),
            specialist_timezone="UTC",
        )
        with pytest.raises(AttributeError):
            snap.price = Decimal("999.00")


# ---------------------------------------------------------------------------
# DefaultCommissionPolicy
# ---------------------------------------------------------------------------

class TestDefaultCommissionPolicy:
    def test_returns_configured_percent(self, settings):
        settings.BOOKING_COMMISSION_PERCENT = 8.0
        policy = DefaultCommissionPolicy()
        import uuid
        result = policy.get_percent(uuid.uuid4(), uuid.uuid4())
        assert result == 8.0

    def test_fallback_to_default(self, settings):
        if hasattr(settings, 'BOOKING_COMMISSION_PERCENT'):
            delattr(settings, 'BOOKING_COMMISSION_PERCENT')
        policy = DefaultCommissionPolicy()
        import uuid
        result = policy.get_percent(uuid.uuid4(), uuid.uuid4())
        assert result == 8.0  # fallback


# ---------------------------------------------------------------------------
# StandardCancellationPolicy
# ---------------------------------------------------------------------------

class TestStandardCancellationPolicy:
    def _future(self, hours: int) -> datetime:
        return datetime.now(tz=timezone.utc) + timedelta(hours=hours)

    def test_free_cancel_24h_ahead(self):
        policy = StandardCancellationPolicy()
        refund = policy.get_refund_percent(self._future(48), "client")
        assert refund == 100.0

    def test_partial_refund_2_to_24h(self):
        policy = StandardCancellationPolicy()
        refund = policy.get_refund_percent(self._future(12), "client")
        assert refund == 50.0

    def test_no_refund_under_2h(self):
        policy = StandardCancellationPolicy()
        refund = policy.get_refund_percent(self._future(1), "client")
        assert refund == 0.0

    def test_specialist_always_full_refund(self):
        policy = StandardCancellationPolicy()
        refund = policy.get_refund_percent(self._future(1), "specialist")
        assert refund == 100.0

    def test_can_cancel_confirmed(self):
        policy = StandardCancellationPolicy()
        # Should not raise
        policy.can_cancel(BookingStatus.CONFIRMED, self._future(48), "client")

    def test_cannot_cancel_completed(self):
        policy = StandardCancellationPolicy()
        with pytest.raises(CancellationNotAllowedError):
            policy.can_cancel(BookingStatus.COMPLETED, self._future(48), "client")

    def test_cannot_cancel_pending(self):
        """PENDING is not in CANCELLABLE_STATUSES (must reach awaiting_payment first)."""
        policy = StandardCancellationPolicy()
        with pytest.raises(CancellationNotAllowedError):
            policy.can_cancel(BookingStatus.PENDING, self._future(48), "client")


# ---------------------------------------------------------------------------
# StandardReschedulePolicy
# ---------------------------------------------------------------------------

class TestStandardReschedulePolicy:
    def _future(self, hours: int) -> datetime:
        return datetime.now(tz=timezone.utc) + timedelta(hours=hours)

    def test_can_reschedule_confirmed_with_notice(self):
        policy = StandardReschedulePolicy()
        # Should not raise
        policy.can_reschedule(
            BookingStatus.CONFIRMED, self._future(6), self._future(48),
        )

    def test_cannot_reschedule_pending(self):
        policy = StandardReschedulePolicy()
        with pytest.raises(RescheduleNotAllowedError):
            policy.can_reschedule(
                BookingStatus.PENDING, self._future(6), self._future(48),
            )

    def test_cannot_reschedule_too_soon(self):
        policy = StandardReschedulePolicy()
        with pytest.raises(RescheduleNotAllowedError, match="4h notice"):
            policy.can_reschedule(
                BookingStatus.CONFIRMED, self._future(2), self._future(48),
            )

    def test_cannot_reschedule_to_past(self):
        policy = StandardReschedulePolicy()
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        with pytest.raises(RescheduleNotAllowedError, match="future"):
            policy.can_reschedule(
                BookingStatus.CONFIRMED, self._future(6), past,
            )


# ---------------------------------------------------------------------------
# DefaultBookingWindowPolicy
# ---------------------------------------------------------------------------

class TestDefaultBookingWindowPolicy:
    def test_valid_booking_window(self):
        policy = DefaultBookingWindowPolicy()
        # 3 hours from now — should be fine
        future = datetime.now(tz=timezone.utc) + timedelta(hours=3)
        policy.validate_booking_window(future)  # Should not raise

    def test_too_soon(self):
        policy = DefaultBookingWindowPolicy()
        soon = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        with pytest.raises(BookingWindowError, match="at least"):
            policy.validate_booking_window(soon)

    def test_too_far(self):
        policy = DefaultBookingWindowPolicy()
        far = datetime.now(tz=timezone.utc) + timedelta(days=90)
        with pytest.raises(BookingWindowError, match="more than"):
            policy.validate_booking_window(far)
