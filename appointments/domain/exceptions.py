"""
Domain exceptions for Booking Engine.
These are business-level errors, not technical ones.
"""


class BookingDomainError(Exception):
    """Base class for all domain errors."""
    pass


class SlotNotAvailableError(BookingDomainError):
    """Raised when the requested slot is already taken or blocked."""
    pass


class ExternalSlotTakenError(SlotNotAvailableError):
    """Slot taken by an external calendar (S3-CAL recheck-at-confirm).

    Subclasses SlotNotAvailableError so existing 409 handlers keep working;
    a distinct type lets callers surface EXTERNAL_SLOT_TAKEN if they want to.
    """
    pass


class InvalidStateTransitionError(BookingDomainError):
    """Raised when a booking state transition is not allowed."""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(
            f"Cannot transition booking from '{current}' to '{target}'"
        )


class SpecialistNotActiveError(BookingDomainError):
    """Raised when the specialist is not accepting bookings."""
    pass


class ServiceNotActiveError(BookingDomainError):
    """Raised when the service is not available for booking."""
    pass


class BookingWindowError(BookingDomainError):
    """Raised when the booking is outside allowed time window."""
    pass


class RescheduleNotAllowedError(BookingDomainError):
    """Raised when reschedule policy does not allow the operation."""
    pass


class CancellationNotAllowedError(BookingDomainError):
    """Raised when cancellation policy does not allow the operation."""
    pass


class BillingEligibilityError(BookingDomainError):
    """C1 — billing refused a NEW booking (subscription past due).

    Carries the machine ``reason`` from the C1 EligibilityResult
    (today only "SUBSCRIPTION_PAST_DUE"). Mapping per C1 privacy rule:
    the internal/backend surface gets 409 SUBSCRIPTION_PAST_DUE, the
    client-facing API gets a generic UNAVAILABLE — the debt reason is
    never disclosed to customers.
    """

    def __init__(self, reason: str = "SUBSCRIPTION_PAST_DUE"):
        self.reason = reason
        super().__init__(reason)


class SalonServiceBookingNotPersistableError(BookingDomainError):
    """AMD-019 stop-condition — a SalonService-catalog booking passed
    resolution and validation (the service IS valid), but the
    Appointment row cannot be persisted: ``Appointment.service`` is a
    mandatory FK to the marketplace ``Service`` model (PROTECT, NOT
    NULL) and writing a SalonService.id there is forbidden (no
    dual-write, no polymorphic schema).

    Raised at the SINGLE persistence boundary in CreateBookingService —
    the point a later owner decision (schema variant A) replaces with
    the real write. Maps to 409 SALON_SERVICE_BOOKING_UNSUPPORTED (NOT
    ServiceNotActiveError — the service is valid)."""
    pass
