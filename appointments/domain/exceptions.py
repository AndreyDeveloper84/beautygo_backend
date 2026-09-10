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


class StaleVersionError(BookingDomainError):
    """Raised when ``expected_version`` no longer matches the locked row.

    Distinct from ``AppointmentTerminalError`` — the appointment is still
    active, but some other write beat this one to it (e.g. another
    reschedule). The caller should refetch and retry with the new version.
    """
    pass


class AppointmentTerminalError(BookingDomainError):
    """Raised when the locked row is terminal (cancelled/completed/no_show).

    Distinct from ``RescheduleNotAllowedError`` — this specifically means
    the appointment reached a terminal state *concurrently*, between the
    pre-lock validation and the row lock (e.g. a racing cancel committed
    first). ``RescheduleNotAllowedError`` covers the non-race case (e.g.
    still PENDING, never confirmed).
    """
    pass


class ExpectedVersionRequiredError(BookingDomainError):
    """Raised when ``expected_version`` is omitted and the temporary
    mobile compatibility gate (``settings.RESCHEDULE_MOBILE_UNVERSIONED_
    ALLOWED``) has been turned off.

    Distinct from ``StaleVersionError`` — this fires when the caller
    provides no version to check at all (the omission itself is
    rejected), not when a provided version fails to match.
    """
    pass


class TenantMismatchError(BookingDomainError):
    """Raised when the locked row's tenant doesn't match the caller's
    tenant context. Views map this to a 404 (info-hiding), mirroring the
    cross-tenant checks in AppointmentViewSet.complete()/no_show()."""
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


class HealthScreeningRequiredError(BookingDomainError):
    """The booking cannot be created without a human health screening.

    Carries a machine ``reason``, and the two values are NOT the same
    situation — that is the whole point of splitting them::

        HEALTH_CHECK_REQUIRED   the catalog says this service needs a
                                screening. The person must pass one.
        HEALTH_CHECK_UNKNOWN    nobody has said anything about this
                                service. The person is waiting on the
                                salon, not on a screening.

    Collapsing the two would cost us the only number that says how much of
    the refusal is our own missing data: on the pilot, 09.09.2026, 95 of 95
    bookable edges of the live salon resolve to UNKNOWN because no template
    is attached, and none of them to REQUIRED. A single counter would have
    read as "the gate fires a lot" and hidden that it fires on ignorance.

    Outward, both may render as one blunt sentence — the customer does not
    need our taxonomy. Inward they must stay apart: the operator handling
    the handoff needs to know whether to run a screening or to go ask the
    salon a question, and those are different jobs.
    """

    REQUIRED = "HEALTH_CHECK_REQUIRED"
    UNKNOWN = "HEALTH_CHECK_UNKNOWN"

    def __init__(self, reason: str = REQUIRED):
        self.reason = reason
        super().__init__(reason)
