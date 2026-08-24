"""
Data Transfer Objects for Booking Engine application layer.

DTOs cross the boundary between API/infrastructure and application services.
They are plain Python dataclasses — no ORM, no Django imports.
All IDs are UUID (matching our project convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from uuid import UUID


# ---------------------------------------------------------------------------
# Input DTOs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateBookingDTO:
    """Input to CreateBookingService."""
    client_id: UUID
    specialist_id: UUID
    service_id: UUID
    start_at: datetime          # Must be UTC timezone-aware
    idempotency_key: str        # Client-provided UUID to prevent duplicates
    # Tenant context the caller is acting in (X-Tenant header at the
    # REST layer; mobile UI tenant switcher). Retained for auditing /
    # future scoping. As of #1014 it no longer gates the grant-on-
    # first-booking rule — that grant keys purely off the specialist's
    # tenant inside the booking transaction (see
    # CreateBookingService._execute_atomic). appointment.tenant_id is
    # always stamped from the specialist's tenant regardless.
    request_tenant_id: Optional[UUID] = None
    # Provider walk-in create path (#1017). Defaults preserve the
    # customer online-payment contract (a Payment row is created and the
    # booking lands in AWAITING_PAYMENT). A provider recording a walk-in
    # passes ``payment_required=False`` + ``confirm_immediately=True`` so
    # the booking skips Payment and lands directly in CONFIRMED (the
    # cash/in-person transaction happens off-platform). ``actor_role``
    # maps to the ADR-0009 envelope actor ("specialist" → "admin").
    payment_required: bool = True
    confirm_immediately: bool = False
    # OperationalActor vocabulary {client|user, specialist, salon, system}
    # — see appointments/domain/value_objects.py. "user" is the legacy
    # spelling of the client actor on this DTO and is kept because the
    # schedule guard below keys off it; the newer paths pass the
    # OperationalActor values directly. DRF-1064 added "salon": a booking
    # the salon records on a customer's behalf.
    actor_role: str = "user"


@dataclass(frozen=True)
class CancelBookingDTO:
    """Input to CancelBookingService."""
    booking_id: UUID
    initiator_user_id: UUID
    # OperationalActor vocabulary — "client" | "specialist" | "salon" |
    # "system". See appointments/domain/value_objects.py.
    initiator_role: str
    reason: Optional[str] = None
    # §3.2 reason_code asserted by a TRUSTED caller, bypassing the
    # role-default. Server-set only: the public cancel path leaves it
    # None and gets the default for its role, because free-text from a
    # client must never drive the attribution enum (see
    # _resolve_cancellation_vocab). The salon surface sets it from a
    # closed allowlist — a salon genuinely knows whether the master is
    # unavailable or the slot was closed, and telling the client "other"
    # when the salon said "мастер заболел" throws that away.
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class RescheduleBookingDTO:
    """Input to RescheduleBookingService."""
    booking_id: UUID
    initiator_user_id: UUID
    new_start_at: datetime      # Must be UTC timezone-aware
    # OperationalActor vocabulary — "client" | "specialist" | "salon" |
    # "system", the same closed set CancelBookingDTO names, so the outbox
    # envelope maps to ADR-0009 actor consistently across both flows.
    # "salon" was omitted from this comment when DRF-1064 introduced it,
    # and the three emit sites downstream had no branch for it either:
    # a reschedule made at the front desk reached every consumer as one
    # the customer made. Default "client" keeps pre-#486 fixtures green.
    initiator_role: str = "client"
    # Wave 1 Simple Reschedule hardening (all optional — omitting them
    # preserves the exact pre-Wave-1 behaviour for any existing caller):
    #
    # Optimistic-concurrency check. When set, the service compares it
    # against the LOCKED row's Appointment.version and raises
    # StaleVersionError on mismatch instead of silently overwriting a
    # change the caller never saw.
    expected_version: Optional[int] = None
    # Defense-in-depth tenant boundary — mirrors AppointmentViewSet
    # .complete()/.no_show()'s post-lock tenant assertion. None means
    # "no tenant context to check against" (e.g. the bot-facing internal
    # endpoint, which has no client tenant scope — same rationale as
    # CreateBookingDTO.request_tenant_id for the bot path).
    tenant_id: Optional[UUID] = None
    # X-Idempotency-Key of the originating request, if any. Stored on
    # the AppointmentRevision audit row for cross-service tracing —
    # NOT used for idempotency itself (that's handled at the view layer
    # via infrastructure/idempotency.py before the service ever runs).
    command_key: Optional[str] = None
    # Request channel — distinct from initiator_role (who acted) in
    # that this captures HOW/WHERE (mobile app vs bot-facing internal
    # API vs a future system-initiated reschedule).
    basis: str = "mobile_app"


@dataclass(frozen=True)
class GetAvailabilityDTO:
    """Input to AvailabilityQueryService."""
    specialist_id: UUID
    target_date: date
    service_id: UUID


@dataclass(frozen=True)
class GetAvailabilityWeekDTO:
    """Input for weekly availability query."""
    specialist_id: UUID
    start_date: date
    service_id: UUID
    days: int = 7


# ---------------------------------------------------------------------------
# Output DTOs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookingResultDTO:
    """Returned by CreateBookingService after successful creation."""
    booking_id: UUID
    status: str
    start_at: datetime
    end_at: datetime
    service_name: str
    duration_minutes: int
    price: str                  # Decimal as string to avoid float precision
    payment_id: Optional[UUID] = None
    payment_client_secret: Optional[str] = None


@dataclass(frozen=True)
class RescheduleResultDTO:
    """Returned by RescheduleBookingService.execute() after success.

    ``version``/``revision_id`` let the API layer echo the exact values
    just written inside the lock (targeted patch — see 03_AGENT
    ...FINAL_TARGETED_PATCH_BEFORE_COMMIT.md item 2) without a second
    query. ``correlation_id`` is the value shared by BOTH the canonical
    ``appointment.rescheduled`` and legacy ``booking.rescheduled``
    events emitted for this command (item 1) — exposed mainly for
    tests/tracing, not required by API consumers.
    """
    booking_id: UUID
    version: int
    revision_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class AvailableSlotDTO:
    """A single available time slot."""
    start_at: datetime          # UTC
    end_at: datetime            # UTC
    start_local: str            # "HH:MM" in specialist's local timezone
    duration_minutes: int


@dataclass(frozen=True)
class DayAvailabilityDTO:
    """Available slots for a single day."""
    date: date
    is_working_day: bool
    slots: list[AvailableSlotDTO] = field(default_factory=list)


@dataclass(frozen=True)
class WeekAvailabilityDTO:
    """Available slots for a date range."""
    specialist_id: UUID
    service_id: UUID
    days: list[DayAvailabilityDTO] = field(default_factory=list)
