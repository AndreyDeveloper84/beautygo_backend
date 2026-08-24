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


# ---------------------------------------------------------------------------
# Operational actor — who performed an action on a booking
# ---------------------------------------------------------------------------

class OperationalActor(str, Enum):
    """Who performed an operational action on a booking.

    ONE closed vocabulary for every actor-bearing field in the booking
    domain: the ``initiator_role`` DTOs, ``AppointmentRevision.actor_role``,
    ``Appointment.completed_by`` / ``no_show_marked_by``, and the
    corresponding ``data`` fields of the outbox events.

    ``SALON`` is the value DRF-1064 adds. Until it existed, a salon
    employee acting on a booking had no honest representation: the closest
    fit was ``SYSTEM`` + a separate ``initiator_user_id``, which files a
    human decision under "automation". That mattered twice over — the
    §3.2 ``reason_code`` default for ``system`` is ``other`` (so the
    client would learn nothing about why their booking moved), and
    DRF-1064's block B introduces a *real* automatic closure, after which
    "the salon closed it" and "the 3-hour sweep closed it" would have been
    the same value.

    Nothing new leaks across the service boundary: both external
    vocabularies already declare a slot for a salon employee and simply
    never produced it —
    ``booking.cancelled.cancelled_by`` (event-contract.md §3.2) is
    ``{user, admin, master, system}``, and the Domain Event Registry's
    payload ``actor`` for ``appointment.rescheduled`` is
    ``user | specialist | admin | owner | system | external_system``.
    See ``envelope_actor_for`` / ``cancelled_by_for`` for the mapping.

    The internal names stay domain-flavoured (``client``, not ``user``;
    ``specialist``, not ``master``) because ``admin`` is already taken
    twice inside Ayla — ``TenantUserRelationship.Role.ADMIN`` and
    ``User.is_platform_admin`` — and reusing it here would conflate the
    role someone holds with the capacity they acted in.
    """
    CLIENT = "client"
    SPECIALIST = "specialist"
    SALON = "salon"
    SYSTEM = "system"


# ADR-0009 envelope ``actor`` is a deliberately coarse three-value enum
# (see infrastructure/outbox/envelope.py VALID_ACTORS). event-contract.md
# §2.2: "The actor field does NOT identify which specific admin. If
# consumers need that, it goes in data." Hence salon and specialist share
# ``admin`` in the envelope and are told apart by the payload field.
#
# ``"user"`` appears alongside ``"client"`` because ``CreateBookingDTO``
# spells the client actor that way and predates this vocabulary. Listed
# explicitly rather than left to the fallback: relying on a default to
# produce the right answer for a value we know about is an accident
# waiting to be broken by the next person who changes the default.
_ENVELOPE_ACTOR: dict[str, str] = {
    OperationalActor.CLIENT.value: "user",
    "user": "user",
    OperationalActor.SPECIALIST.value: "admin",
    OperationalActor.SALON.value: "admin",
    OperationalActor.SYSTEM.value: "system",
}

# event-contract.md §3.2 ``cancelled_by`` — {user, admin, master, system}.
# ``admin`` has been in that closed set since the contract was written and
# no Ayla call site produced it until DRF-1064. The bot consumer already
# branches on it (§3.2 consumer contract step 3: notify the customer when
# cancelled_by ∈ {admin, master, system}), so a salon-initiated
# cancellation lands in the right branch with no bot-side change.
_CANCELLED_BY: dict[str, str] = {
    OperationalActor.CLIENT.value: "user",
    OperationalActor.SPECIALIST.value: "master",
    OperationalActor.SALON.value: "admin",
    OperationalActor.SYSTEM.value: "system",
}


# event-contract.md §3.3 ``rescheduled_by`` — {user, admin, master, system}.
# ``admin`` has been in that closed set since the contract was written and,
# exactly as with ``cancelled_by`` before DRF-1064, no Ayla call site
# produced it. A salon reschedule was reported as ``user``: the front desk
# moved somebody's appointment and the event said the customer did.
_RESCHEDULED_BY: dict[str, str] = {
    OperationalActor.CLIENT.value: "user",
    "user": "user",                # CreateBookingDTO's legacy spelling
    OperationalActor.SPECIALIST.value: "master",
    OperationalActor.SALON.value: "admin",
    OperationalActor.SYSTEM.value: "system",
}


# Ayla Domain Event Registry v0.4 §6.3 (registered — AYLA-DEC-0022 п.9)
# payload ``actor`` enum: user | specialist | admin | owner | system |
# external_system. Distinct from the ADR-0009 envelope ``actor`` above:
# the envelope's is a coarse three-value routing bucket shared by every
# outbox event, while this is the literal initiator role, so a
# specialist-initiated reschedule says ``specialist`` here and ``admin``
# there. The envelope value does NOT substitute for this one — they
# answer different questions.
_REGISTRY_ACTOR: dict[str, str] = {
    OperationalActor.CLIENT.value: "user",
    "user": "user",
    OperationalActor.SPECIALIST.value: "specialist",
    OperationalActor.SALON.value: "admin",
    OperationalActor.SYSTEM.value: "system",
}


# event-contract.md §3.1 ``booking.created.source`` — the coarse origin
# channel: {mobile_app, admin_console, automation, yclients_sync}. This is
# the ``origin`` that Ayla MVP Appointment Contract §10 names when it says
# manual salon booking "uses the same Appointment Domain and lifecycle…
# It differs by `origin`, actor and authorization context".
#
# ``walk_in`` for a master-recorded booking predates the enum and is kept
# as-is: the consumer stores `source` verbatim and changing it now would
# reclassify every historical walk-in.
_BOOKING_SOURCE: dict[str, str] = {
    OperationalActor.CLIENT.value: "mobile_app",
    "user": "mobile_app",          # CreateBookingDTO's legacy spelling
    OperationalActor.SPECIALIST.value: "walk_in",
    OperationalActor.SALON.value: "admin_console",
    OperationalActor.SYSTEM.value: "automation",
}


def envelope_actor_for(actor: str) -> str:
    """Map an :class:`OperationalActor` value → ADR-0009 envelope ``actor``.

    Unknown values fall back to ``"user"`` rather than raising: the
    envelope builder validates its own enum, and an emit site should not
    500 a committed domain change over an attribution label.
    """
    return _ENVELOPE_ACTOR.get(actor, "user")


def cancelled_by_for(actor: str) -> str:
    """Map an :class:`OperationalActor` value → §3.2 ``cancelled_by``."""
    return _CANCELLED_BY.get(actor, "user")


def rescheduled_by_for(actor: str) -> str:
    """Map an :class:`OperationalActor` value → §3.3 ``rescheduled_by``."""
    return _RESCHEDULED_BY.get(actor, "user")


def registry_actor_for(actor: str) -> str:
    """Map an :class:`OperationalActor` value → registry §6.3 ``actor``."""
    return _REGISTRY_ACTOR.get(actor, "user")


def booking_source_for(actor: str) -> str:
    """Map an :class:`OperationalActor` value → §3.1 ``source``."""
    return _BOOKING_SOURCE.get(actor, "mobile_app")


# §3.2 ``reason_code`` values a salon employee may legitimately assert
# about their own booking. Deliberately NOT the full enum: `user_*` codes
# are the client's business and `payment_hold_expired` is the payment
# system's fact, so letting the salon claim either would let one party
# author another's attribution. The remaining three are things the salon
# genuinely knows.
SALON_CANCELLATION_REASON_CODES: FrozenSet[str] = frozenset({
    "master_unavailable",
    "tenant_closed_slot",
    "other",
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
