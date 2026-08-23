"""What breaks if this time is closed — read-side preview (DRF-1062 §C).

Today an administrator who tries to record a master's absence over live
bookings gets 409 HAS_ACTIVE_APPOINTMENTS and nothing else: no list, no
way forward. As data protection that is right — nobody should be able to
strand a booked client silently. As a product it is a dead end in exactly
the situation the feature exists for, which is a master falling ill.

This service supplies the missing half: the bookings a proposed absence
would hit, so the administrator can decide about each one before the
absence is recorded.

Two properties matter.

*Times are rendered in the specialist's timezone.* DRF-1071 found the
records list printing UTC — a client booked for 14:00 MSK reading 11:00.
An operator deciding whose appointment to cancel must not be handed the
same trap.

*The set is fingerprinted.* Between previewing and confirming, a client
can book into the very window under discussion — on 2026-08-14 someone
created two bookings inside one minute. ``impact_token`` lets the write
side detect that the world moved and re-ask, the same way reschedule uses
``expected_version``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES


@dataclass(frozen=True)
class AffectedBooking:
    """One live booking inside the proposed absence.

    Carries no client identity beyond the opaque id: deciding what to do
    with a booking needs its time, service and money, not who it belongs
    to. Keeping it that way means this surface never becomes a way around
    the DRF-1039 rule that the salon reaches clients through Ayla.
    """

    appointment_id: str
    version: int
    status: str
    start_at_local: str
    end_at_local: str
    timezone_name: str
    service_name: str
    duration_minutes: int | None
    price: str
    payment_status: str | None
    refund_percent_if_cancelled: float


@dataclass(frozen=True)
class ScheduleImpact:
    specialist_id: str
    start_at: str
    end_at: str
    timezone_name: str
    bookings: list[AffectedBooking] = field(default_factory=list)
    impact_token: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.bookings


def _fingerprint(specialist_id, start_at, end_at, rows) -> str:
    """Stable hash of (window, {booking, version}).

    Version is included so an appointment rescheduled between preview and
    confirm invalidates the token even though its id did not change.

    Timestamps are normalised to UTC epoch seconds rather than hashed as
    strings: the preview parses them out of a query string and the
    confirmation out of a JSON body, and those two paths render the same
    instant with different offsets ("+00:00" vs "+03:00"). Hashing the
    representation would make every honest confirmation look like a race.
    """
    parts = [
        str(specialist_id),
        str(start_at.astimezone(timezone.utc).timestamp()),
        str(end_at.astimezone(timezone.utc).timestamp()),
    ]
    parts += sorted(f"{row.id}:{row.version}" for row in rows)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def count_active_bookings_in_window(specialist, start_at: datetime, end_at: datetime) -> int:
    """Live bookings of this master overlapping ``[start_at, end_at)``.

    DRF-1297 B-4. This exact query existed twice, character for
    character, as an inline block inside two time-off endpoints
    (``users/schedule_api.py`` and ``users/internal_schedule_api.py``).
    Both asked the one question a cheap availability guard needs — *does
    closing this window strand anyone?* — and both answered it by
    counting rather than by listing, because the answer is a refusal and
    not a preview.

    Scope, stated so it is not widened by accident: this is **simple
    interval overlap**, the same predicate ``get_schedule_impact`` uses
    below and nothing more. It deliberately does NOT answer the shrinking
    question — *which bookings fall OUTSIDE a proposed frame* — which is
    a different comparison (old effective frame vs new, unrolled over a
    horizon) and is why the weekly-template guard is a separate piece of
    work rather than one more caller of this function. A booking may sit
    outside working hours perfectly legally (walk-ins and salon-made
    bookings skip the frame check by design), so "outside the new frame"
    is not evidence of anything on its own.

    Not transactional and takes no lock, exactly as the two originals
    were: a booking can still be created between this count and the write
    that follows. It catches an administrator's mistake, which is what it
    is for; it is not a serialisation guarantee and must not be described
    as one.
    """
    from appointments.models import Appointment

    return (
        Appointment.objects
        .filter(
            specialist=specialist,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=end_at,
            end_datetime__gt=start_at,
        )
        .count()
    )


def local_day_window_utc(
    local_date,
    tz: ZoneInfo,
    start_time=None,
    end_time=None,
) -> tuple[datetime, datetime]:
    """The UTC instants bounding a local date, or a slice of one.

    Built from two dates rather than "+24h" for the full-day case, and
    from the calendar date for a partial one, so a DST transition keeps
    the window aligned to the day a human means. Same form as
    ``TenantClosureBusyIntervalProvider``, which is the read-side of the
    same conversion -- the two must not disagree about which instants a
    closed Tuesday covers.
    """
    from datetime import time as _time

    if start_time is None or end_time is None:
        local_start = datetime.combine(local_date, _time(0, 0), tzinfo=tz)
        local_end = datetime.combine(
            local_date + timedelta(days=1), _time(0, 0), tzinfo=tz,
        )
    else:
        local_start = datetime.combine(local_date, start_time, tzinfo=tz)
        local_end = datetime.combine(local_date, end_time, tzinfo=tz)

    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def get_schedule_impact(
    specialist,
    start_at: datetime,
    end_at: datetime,
) -> ScheduleImpact:
    """Active bookings overlapping [start_at, end_at) for this specialist."""
    from appointments.models import Appointment
    from appointments.domain.policies import ForceFullRefundCancellationPolicy

    tz = ZoneInfo(specialist.timezone)
    policy = ForceFullRefundCancellationPolicy()

    rows = list(
        Appointment.objects
        .filter(
            specialist=specialist,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=end_at,
            end_datetime__gt=start_at,
        )
        .select_related('service', 'salon_service')
        .prefetch_related('payments')
        .order_by('start_datetime')
    )

    bookings = []
    for row in rows:
        payment = row.payments.order_by('-created_at').first()
        bookings.append(AffectedBooking(
            appointment_id=str(row.id),
            version=row.version,
            status=row.status,
            start_at_local=row.start_datetime.astimezone(tz).isoformat(),
            end_at_local=row.end_datetime.astimezone(tz).isoformat(),
            timezone_name=specialist.timezone,
            service_name=row.snapshot_service_name or "",
            duration_minutes=row.snapshot_duration_minutes,
            price=str(row.snapshot_price if row.snapshot_price is not None else row.price),
            payment_status=payment.status if payment else None,
            # The salon closing time is never the client's fault, so the
            # cancellation offered here is always the no-fault one.
            refund_percent_if_cancelled=policy.get_refund_percent(
                booking_start_at=row.start_datetime,
                initiator="system",
            ),
        ))

    return ScheduleImpact(
        specialist_id=str(specialist.id),
        start_at=start_at.isoformat(),
        end_at=end_at.isoformat(),
        timezone_name=specialist.timezone,
        bookings=bookings,
        impact_token=_fingerprint(specialist.id, start_at, end_at, rows),
    )
