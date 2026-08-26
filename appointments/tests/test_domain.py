"""Unit tests for domain layer — pure Python, no DB required."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from appointments.application.dto import CancelBookingDTO
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
    UnknownInitiatorError,
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
            platform_fee=Decimal("90.00"),
            specialist_timezone="Europe/Moscow",
            buffer_after_minutes=10,
        )
        # Flat 90₽ fee (AYLA-DEC-0001), income is the remainder.
        assert snap.platform_fee == Decimal("90.00")
        assert snap.specialist_income == Decimal("1910.00")
        # commission_percent is the derived EFFECTIVE rate (analytics only)
        assert snap.commission_percent == Decimal("4.50")
        assert snap.service_name == "Маникюр"
        assert snap.buffer_after_minutes == 10

    def test_fee_capped_at_price(self):
        """Degenerate sub-90₽ service: fee is capped so income never
        goes negative (data contract §1)."""
        snap = BookingSnapshot.create(
            service_name="Test",
            duration_minutes=30,
            price=Decimal("50.00"),
            platform_fee=Decimal("90.00"),
            specialist_timezone="UTC",
        )
        assert snap.platform_fee == Decimal("50.00")
        assert snap.specialist_income == Decimal("0.00")

    def test_snapshot_is_immutable(self):
        snap = BookingSnapshot.create(
            service_name="Test",
            duration_minutes=30,
            price=Decimal("500.00"),
            platform_fee=Decimal("90.00"),
            specialist_timezone="UTC",
        )
        with pytest.raises(AttributeError):
            snap.price = Decimal("999.00")


# ---------------------------------------------------------------------------
# DefaultCommissionPolicy
# ---------------------------------------------------------------------------

class TestDefaultCommissionPolicy:
    def test_returns_configured_flat_fee(self, settings):
        settings.BOOKING_PLATFORM_FEE_RUB = "90.00"
        policy = DefaultCommissionPolicy()
        result = policy.get_platform_fee(Decimal("2000.00"))
        assert result == Decimal("90.00")

    def test_fee_independent_of_price(self, settings):
        """Flat fee (D1): same 90₽ for a 500₽ and a 5000₽ service."""
        settings.BOOKING_PLATFORM_FEE_RUB = "90.00"
        policy = DefaultCommissionPolicy()
        assert policy.get_platform_fee(Decimal("500.00")) == Decimal("90.00")
        assert policy.get_platform_fee(Decimal("5000.00")) == Decimal("90.00")

    def test_fallback_to_default(self, settings):
        if hasattr(settings, 'BOOKING_PLATFORM_FEE_RUB'):
            delattr(settings, 'BOOKING_PLATFORM_FEE_RUB')
        policy = DefaultCommissionPolicy()
        result = policy.get_platform_fee(Decimal("2000.00"))
        assert result == Decimal("90.00")  # fallback


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

    def test_salon_always_full_refund(self):
        policy = StandardCancellationPolicy()
        refund = policy.get_refund_percent(self._future(1), "salon")
        assert refund == 100.0

    def test_system_initiator_is_no_fault_full_refund(self):
        """``system`` is a KNOWN no-fault initiator, not a client-scale one.

        ``schedule_impact_service`` passes ``"system"`` for salon closures
        and documents the outcome as «always the no-fault one» — yet before
        DRF-1156 the value was absent from the provider set and silently
        took the client's time scale (0% inside 2h). The policy now says
        so explicitly instead of letting the fall-through decide.
        """
        policy = StandardCancellationPolicy()
        assert policy.get_refund_percent(self._future(1), "system") == 100.0

    def test_legacy_user_spelling_gets_the_client_scale(self):
        """``user`` is the pre-vocabulary spelling of the client actor —
        known, and priced by time exactly like ``client``."""
        policy = StandardCancellationPolicy()
        assert policy.get_refund_percent(self._future(12), "user") == 50.0

    def test_unknown_initiator_refund_raises(self):
        """DRF-1156: the policy has no «I don't know» case — an unknown
        initiator must refuse, not silently take the client scale down
        to 0%. ``receptionist`` is the salon-admin role that does not
        exist in the vocabulary yet."""
        policy = StandardCancellationPolicy()
        with pytest.raises(UnknownInitiatorError):
            policy.get_refund_percent(self._future(48), "receptionist")

    def test_unknown_initiator_can_cancel_raises(self):
        """Same gate on the guard: an unknown actor must not inherit the
        client's rules there either."""
        policy = StandardCancellationPolicy()
        with pytest.raises(UnknownInitiatorError):
            policy.can_cancel(BookingStatus.CONFIRMED, self._future(48), "receptionist")

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
# CancelBookingDTO — initiator validation (DRF-1156)
# ---------------------------------------------------------------------------

class TestCancelBookingDTOInitiator:
    """The DTO's own comment declares a closed vocabulary
    ({client, specialist, salon, system}); DRF-1156 makes it real.

    An unvalidated string was the mine: any new caller spelling
    (``receptionist``, ``admin``, ``owner``) would sail through to the
    refund policy. The DTO refuses it at construction — before any
    money math runs.
    """

    def _dto(self, initiator_role: str) -> CancelBookingDTO:
        return CancelBookingDTO(
            booking_id=uuid4(),
            initiator_user_id=uuid4(),
            initiator_role=initiator_role,
        )

    @pytest.mark.parametrize("role", ["client", "specialist", "salon", "system"])
    def test_known_vocabulary_accepted(self, role: str):
        assert self._dto(role).initiator_role == role

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError, match="initiator_role"):
            self._dto("receptionist")


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
