"""
Domain policies for Booking Engine.

Policies are pure business rules — no DB access, no HTTP calls.
They take data in, return decisions out. Fully unit-testable.

Design principle (Open/Closed):
New business rules are added by implementing new Policy classes,
not by modifying existing ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID

from django.conf import settings

from .exceptions import (
    BookingWindowError,
    CancellationNotAllowedError,
    RescheduleNotAllowedError,
)
from .value_objects import BookingStatus


# ---------------------------------------------------------------------------
# Protocols (interfaces) — Dependency Inversion in action
# ---------------------------------------------------------------------------

@runtime_checkable
class CommissionPolicy(Protocol):
    """Determines platform commission for a booking."""

    def get_percent(self, client_id: UUID, specialist_id: UUID) -> float:
        ...


@runtime_checkable
class CancellationPolicy(Protocol):
    """Determines whether a booking can be cancelled and if a refund applies."""

    def can_cancel(
        self,
        booking_status: BookingStatus,
        booking_start_at: datetime,
        initiator: str,  # "client" | "specialist" | "system"
    ) -> None:
        """Raises CancellationNotAllowedError if cancellation is not permitted."""
        ...

    def get_refund_percent(
        self,
        booking_start_at: datetime,
        initiator: str,
    ) -> float:
        """Returns refund percentage (0-100)."""
        ...


@runtime_checkable
class ReschedulePolicy(Protocol):
    """Determines whether a booking can be rescheduled."""

    def can_reschedule(
        self,
        booking_status: BookingStatus,
        booking_start_at: datetime,
        new_start_at: datetime,
    ) -> None:
        """Raises RescheduleNotAllowedError if reschedule is not permitted."""
        ...


@runtime_checkable
class BookingWindowPolicy(Protocol):
    """Controls how far in the future bookings can be made."""

    def validate_booking_window(self, requested_start_at: datetime) -> None:
        """Raises BookingWindowError if the date is out of allowed range."""
        ...


# ---------------------------------------------------------------------------
# Default implementations
# ---------------------------------------------------------------------------

class DefaultCommissionPolicy:
    """
    Configurable commission rate. Reads from settings with 8% fallback.
    In v2, this can be replaced with tiered/specialist-specific rates.
    """

    def get_percent(self, client_id: UUID, specialist_id: UUID) -> float:
        return float(getattr(settings, 'BOOKING_COMMISSION_PERCENT', 8.0))


class StandardCancellationPolicy:
    """
    Cancellation rules:
    - Free cancellation up to 24h before the appointment
    - 50% fee for cancellation 2-24h before
    - No refund for cancellation < 2h before
    - Specialist-initiated cancellation -> always full refund

    These rules are configurable per marketplace requirements.
    """
    FREE_CANCEL_HOURS = 24
    PARTIAL_REFUND_HOURS = 2
    PARTIAL_REFUND_PERCENT = 50.0

    # States from which cancellation is permitted
    CANCELLABLE_STATUSES = frozenset({
        BookingStatus.AWAITING_PAYMENT,
        BookingStatus.CONFIRMED,
    })

    def can_cancel(
        self,
        booking_status: BookingStatus,
        booking_start_at: datetime,
        initiator: str,
    ) -> None:
        if booking_status not in self.CANCELLABLE_STATUSES:
            raise CancellationNotAllowedError(
                f"Cannot cancel booking with status '{booking_status.value}'"
            )

        # Specialists can always cancel (their time, their call)
        if initiator == "specialist":
            return

        now = datetime.now(tz=timezone.utc)
        hours_until = (booking_start_at - now).total_seconds() / 3600

        if hours_until < 0:
            raise CancellationNotAllowedError(
                "Cannot cancel a booking that has already started"
            )

    def get_refund_percent(
        self,
        booking_start_at: datetime,
        initiator: str,
    ) -> float:
        if initiator == "specialist":
            return 100.0

        now = datetime.now(tz=timezone.utc)
        hours_until = (booking_start_at - now).total_seconds() / 3600

        if hours_until >= self.FREE_CANCEL_HOURS:
            return 100.0
        elif hours_until >= self.PARTIAL_REFUND_HOURS:
            return self.PARTIAL_REFUND_PERCENT
        else:
            return 0.0


class StandardReschedulePolicy:
    """
    Reschedule is allowed if:
    - Booking is in CONFIRMED status
    - Current booking starts more than 4h from now
    - New slot is not in the past
    """
    MIN_HOURS_BEFORE_RESCHEDULE = 4

    RESCHEDULABLE_STATUSES = frozenset({BookingStatus.CONFIRMED})

    def can_reschedule(
        self,
        booking_status: BookingStatus,
        booking_start_at: datetime,
        new_start_at: datetime,
    ) -> None:
        if booking_status not in self.RESCHEDULABLE_STATUSES:
            raise RescheduleNotAllowedError(
                f"Cannot reschedule booking with status '{booking_status.value}'"
            )

        now = datetime.now(tz=timezone.utc)
        hours_until = (booking_start_at - now).total_seconds() / 3600

        if hours_until < self.MIN_HOURS_BEFORE_RESCHEDULE:
            raise RescheduleNotAllowedError(
                f"Reschedule requires at least {self.MIN_HOURS_BEFORE_RESCHEDULE}h notice"
            )

        if new_start_at <= now:
            raise RescheduleNotAllowedError("New slot must be in the future")


class DefaultBookingWindowPolicy:
    """
    Clients can book slots between MIN_AHEAD_MINUTES and MAX_AHEAD_DAYS from now.
    Reads thresholds from settings with sensible fallbacks.
    """

    def validate_booking_window(self, requested_start_at: datetime) -> None:
        min_ahead = int(getattr(settings, 'BOOKING_MIN_AHEAD_MINUTES', 60))
        max_ahead = int(getattr(settings, 'BOOKING_MAX_AHEAD_DAYS', 60))

        now = datetime.now(tz=timezone.utc)
        min_allowed = now + timedelta(minutes=min_ahead)
        max_allowed = now + timedelta(days=max_ahead)

        if requested_start_at < min_allowed:
            raise BookingWindowError(
                f"Booking must be at least {min_ahead} minutes in the future"
            )

        if requested_start_at > max_allowed:
            raise BookingWindowError(
                f"Booking cannot be more than {max_ahead} days in advance"
            )
