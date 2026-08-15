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
from decimal import Decimal
from typing import Protocol, runtime_checkable

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
    """Determines the platform fee for a booking."""

    def get_platform_fee(self, price: Decimal) -> Decimal:
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
    """Flat platform fee per booking (AYLA-DEC-0001/D1: 90₽).

    Replaces the pre-pilot 8% split. The fee is independent of who books
    or how much the service costs; the BookingSnapshot caps it at the
    price so specialist income never goes negative (data contract §1).
    """

    def get_platform_fee(self, price: Decimal) -> Decimal:
        return Decimal(str(getattr(settings, 'BOOKING_PLATFORM_FEE_RUB', '90.00')))


class StandardCancellationPolicy:
    """
    Cancellation rules:
    - Free cancellation up to 24h before the appointment
    - 50% fee for cancellation 2-24h before
    - No refund for cancellation < 2h before
    - Provider-initiated cancellation -> always full refund

    These rules are configurable per marketplace requirements.
    """
    FREE_CANCEL_HOURS = 24
    PARTIAL_REFUND_HOURS = 2
    PARTIAL_REFUND_PERCENT = 50.0

    # Initiators on the provider's side of the transaction. The fee
    # schedule below prices the CLIENT's change of mind; charging it when
    # the salon or the master cancels would bill a customer for a
    # decision that was never theirs. DRF-1064 added "salon": a front
    # desk cancelling an hour before would otherwise have handed the
    # client a 0% refund.
    PROVIDER_INITIATORS = frozenset({"specialist", "salon"})

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

        # The provider side can always cancel (their time, their call)
        if initiator in self.PROVIDER_INITIATORS:
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
        if initiator in self.PROVIDER_INITIATORS:
            return 100.0

        now = datetime.now(tz=timezone.utc)
        hours_until = (booking_start_at - now).total_seconds() / 3600

        if hours_until >= self.FREE_CANCEL_HOURS:
            return 100.0
        elif hours_until >= self.PARTIAL_REFUND_HOURS:
            return self.PARTIAL_REFUND_PERCENT
        else:
            return 0.0


class ForceFullRefundCancellationPolicy:
    """No-fault cancellation policy used by cascade flows.

    #246 Q2 (founder ack 2026-05-26): when a specialist leaves a
    tenant, all of their active bookings must be cancelled with a full
    refund and no penalty — regardless of how close to start_at the
    booking is. The standard policy's time-based fee schedule does not
    apply: the customer did nothing wrong.

    Differs from ``StandardCancellationPolicy`` in two ways:

    1. CANCELLABLE_STATUSES extends to PENDING (specialist hasn't yet
       confirmed; cascade kills it regardless).
    2. ``get_refund_percent`` is ALWAYS 100.0.
    3. ``can_cancel`` skips the "already-started" guard — the cascade
       layer is responsible for filtering past-start bookings out of
       the input set (those should be marked completed/no_show via
       different flows, not retroactively cancelled).

    Use ONLY from system-initiated cascade contexts; never expose to
    end users (would defeat the whole point of the standard fee
    schedule).
    """
    CANCELLABLE_STATUSES = frozenset({
        BookingStatus.PENDING,
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
        # Intentionally no time-window check — see class docstring.

    def get_refund_percent(
        self,
        booking_start_at: datetime,
        initiator: str,
    ) -> float:
        return 100.0


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
