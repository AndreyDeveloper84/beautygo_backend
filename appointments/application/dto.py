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
    # #246 sub-phase 1.D Variant E: when the caller is in a specific
    # tenant context (X-Tenant header at the REST layer; mobile UI
    # tenant switcher), pass it through so the service can invisibly
    # grant TUR(client, specialist.tenant) inside the booking
    # transaction. None = caller is in customer-wide context — no
    # grant needed (appointment.tenant_id stamped from specialist
    # per legacy behavior).
    request_tenant_id: Optional[UUID] = None


@dataclass(frozen=True)
class CancelBookingDTO:
    """Input to CancelBookingService."""
    booking_id: UUID
    initiator_user_id: UUID
    initiator_role: str         # "client" | "specialist" | "system"
    reason: Optional[str] = None


@dataclass(frozen=True)
class RescheduleBookingDTO:
    """Input to RescheduleBookingService."""
    booking_id: UUID
    initiator_user_id: UUID
    new_start_at: datetime      # Must be UTC timezone-aware
    # initiator_role: "client" | "specialist" | "system" — matches the
    # CancelBookingDTO contract so the outbox envelope can map to
    # ADR-0009 actor consistently across both flows. Default "client"
    # keeps pre-#486 test fixtures green.
    initiator_role: str = "client"


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
