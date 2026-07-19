"""
Domain value objects and state machine for Booking Engine.

Key design decisions:
- TimeInterval is immutable (frozen dataclass) — value object semantics
- BookingStatus is an enum with explicit transition rules
- All time comparisons work in UTC; local time is only for display
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import FrozenSet

from .exceptions import InvalidStateTransitionError


# ---------------------------------------------------------------------------
# TimeInterval — core value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=False)
class TimeInterval:
    """
    An immutable time interval in UTC.

    Invariants:
    - start_at < end_at (enforced at construction)
    - Both datetimes must be timezone-aware
    """
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        if self.start_at >= self.end_at:
            raise ValueError(
                f"start_at must be before end_at: {self.start_at} >= {self.end_at}"
            )
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("TimeInterval requires timezone-aware datetimes")

    def overlaps(self, other: TimeInterval) -> bool:
        """
        Standard overlap check: two intervals overlap if one starts before
        the other ends AND the other starts before this one ends.

        Uses strict inequality — touching intervals (end == start) do NOT overlap.
        This is intentional: back-to-back appointments should be allowed.
        """
        return self.start_at < other.end_at and other.start_at < self.end_at

    def duration_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds() / 60)

    def __repr__(self) -> str:
        return (
            f"TimeInterval({self.start_at.isoformat()} -> {self.end_at.isoformat()})"
        )


# ---------------------------------------------------------------------------
# Booking state machine
# ---------------------------------------------------------------------------

class BookingStatus(str, Enum):
    """
    Booking lifecycle states.

    Allowed transitions (enforced by BookingStateMachine):

        pending -> awaiting_payment -> confirmed -> completed
                                    |-> cancelled
        confirmed -> cancelled
        confirmed -> no_show
        no_show / completed / cancelled -> terminal (no further transitions)
    """
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


# Explicit transition table — single source of truth for allowed moves
_BOOKING_TRANSITIONS: dict[BookingStatus, FrozenSet[BookingStatus]] = {
    BookingStatus.PENDING: frozenset({BookingStatus.AWAITING_PAYMENT, BookingStatus.CANCELLED}),
    BookingStatus.AWAITING_PAYMENT: frozenset({BookingStatus.CONFIRMED, BookingStatus.CANCELLED}),
    BookingStatus.CONFIRMED: frozenset({BookingStatus.COMPLETED, BookingStatus.CANCELLED, BookingStatus.NO_SHOW}),
    # Terminal states — no transitions allowed
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.CANCELLED: frozenset(),
    BookingStatus.NO_SHOW: frozenset(),
}

# Statuses that "hold" a slot (block future bookings)
ACTIVE_BOOKING_STATUSES: FrozenSet[BookingStatus] = frozenset({
    BookingStatus.PENDING,
    BookingStatus.AWAITING_PAYMENT,
    BookingStatus.CONFIRMED,
})


class BookingStateMachine:
    """
    Enforces state transitions.

    Why a separate class and not a method on the model?
    -> Single Responsibility: the model stores data, the state machine
      enforces business rules. This also makes the state machine
      independently unit-testable.
    """

    @staticmethod
    def transition(current: BookingStatus, target: BookingStatus) -> BookingStatus:
        allowed = _BOOKING_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise InvalidStateTransitionError(current.value, target.value)
        return target

    @staticmethod
    def can_transition(current: BookingStatus, target: BookingStatus) -> bool:
        allowed = _BOOKING_TRANSITIONS.get(current, frozenset())
        return target in allowed

    @staticmethod
    def is_terminal(status: BookingStatus) -> bool:
        return not _BOOKING_TRANSITIONS.get(status, frozenset())


# ---------------------------------------------------------------------------
# Booking snapshot — immutable record of financials at booking time
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookingSnapshot:
    """
    Captures the financial and temporal state at booking creation.

    Why store this? Service price can change, specialist can leave the platform.
    We need a reliable record of what was agreed at booking time.
    This is the source of truth for any future disputes or refunds.
    """
    service_name: str
    service_duration_minutes: int
    price: Decimal
    commission_percent: Decimal
    specialist_income: Decimal
    platform_fee: Decimal
    specialist_timezone: str  # IANA timezone string, e.g. "Europe/Moscow"
    buffer_after_minutes: int

    @classmethod
    def create(
        cls,
        service_name: str,
        duration_minutes: int,
        price: Decimal,
        platform_fee: Decimal,
        specialist_timezone: str,
        buffer_after_minutes: int = 0,
    ) -> BookingSnapshot:
        # Flat platform fee (AYLA-DEC-0001/D1: 90₽ per successful booking).
        # Capped at the price so specialist_income stays non-negative
        # (data contract §1: negative amounts are forbidden) — degenerate
        # sub-90₽ services yield zero income rather than a negative one.
        platform_fee = min(platform_fee, price).quantize(Decimal("0.01"))
        specialist_income = price - platform_fee
        # Legacy analytics column: the EFFECTIVE rate derived from the
        # flat fee (90₽ of 2000₽ → 4.50). Informational only — it no
        # longer drives the fee math.
        commission_percent = (
            (platform_fee / price * 100).quantize(Decimal("0.01"))
            if price else Decimal("0.00")
        )
        return cls(
            service_name=service_name,
            service_duration_minutes=duration_minutes,
            price=price,
            commission_percent=commission_percent,
            specialist_income=specialist_income,
            platform_fee=platform_fee,
            specialist_timezone=specialist_timezone,
            buffer_after_minutes=buffer_after_minutes,
        )
