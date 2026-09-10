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
from contextlib import contextmanager
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


class ScheduleShrinkConflict(Exception):
    """A proposed frame change would leave live bookings outside the frame.

    Carries the count only. The refusal is the same plain
    ``HAS_ACTIVE_APPOINTMENTS`` 409 the rest of this surface returns —
    no impact preview, no resolution path, nothing cancelled or moved.
    """

    def __init__(self, stranded: int) -> None:
        super().__init__(f"{stranded} active appointment(s) would be stranded")
        self.stranded = stranded


def _fitting_booking_ids(specialist) -> set:
    """Ids of this master's live FUTURE bookings that fit their day's frame.

    The predicate is :func:`check_schedule_frame` — the SAME function the
    booking write path uses — on purpose. A second implementation of
    "inside working hours" is precisely the divergence DRF-1062 closed
    when it stopped the frame being enforced on the read path only; this
    guard must not reopen it from the other end.

    **Everything future, not just the booking horizon.**
    ``BOOKING_MAX_AHEAD_DAYS`` governs how far ahead a booking may be
    MADE; it says nothing about which existing bookings deserve
    protection. Bookings past it exist — the salon and walk-in paths do
    not consult it — and a client booked for March is stranded by a
    shrink exactly as much as one booked for next week. Bounding this
    scan by that setting would have made the guard silently miss them,
    which is how it was written first and what the two recorded baselines
    caught: their booking sits 84 days out.

    Cost is one frame resolution per future booking, twice per write.
    Deliberate: grouping by date would mean re-implementing the fit test
    instead of calling the authoritative one, and this is a rare
    administrative write, not a hot path.
    """
    from appointments.application.services._booking_guards import check_schedule_frame
    from appointments.domain.exceptions import SlotNotAvailableError
    from appointments.models import Appointment
    from appointments.domain.value_objects import TimeInterval

    now = datetime.now(timezone.utc)

    fitting = set()
    bookings = (
        Appointment.objects
        .filter(
            specialist=specialist,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            end_datetime__gt=now,
        )
        .only("id", "start_datetime", "end_datetime")
    )
    for booking in bookings:
        try:
            check_schedule_frame(
                specialist.id,
                TimeInterval(start_at=booking.start_datetime, end_at=booking.end_datetime),
            )
        except SlotNotAvailableError:
            continue
        fitting.add(booking.id)
    return fitting


@contextmanager
def refuse_if_the_change_strands_bookings(specialist):
    """Refuse a frame change that displaces a booking which used to fit.

    DRF-1297 B-4, the half ``_refuse_if_bookings_are_stranded`` explicitly
    could not answer: **shrinking** the weekly template or trimming the
    hours of a working-day override.

    # Why a before/after measurement and not a check

    "Any booking outside the new frame is a conflict" is the obvious rule
    and it is wrong. A booking may sit outside working hours perfectly
    legally — walk-ins and salon-made bookings skip the frame check by
    design (``create_booking_service``) — so being outside the NEW frame
    proves nothing on its own. What makes a booking *displaced* is that it
    was inside the OLD frame and is not inside the new one.

    Answering that needs the old effective frame and the new one, and the
    only honest way to get the new one is to apply the change and ask the
    authoritative resolver again. So this is a context manager: measure,
    let the caller write, measure again, and raise if anything moved from
    fitting to not-fitting. Wrap it in ``transaction.atomic`` and the
    raise rolls the write back.

    The alternative — computing the proposed frame here — would mean a
    second implementation of "what working hours mean" living next to the
    resolver, which is the exact divergence DRF-1062 was created to end.

    # Named limits, so the guard is not read as more than it is

    **Not transactional by itself and takes no lock**, exactly like the
    date-bounded guard it stands beside. A booking created between the
    two measurements is invisible to it. It catches an administrator
    shrinking a schedule over a client they forgot about; it is not a
    serialisation guarantee and must not be described as one.

    **Only this specialist.** A tenant-wide closure is a different
    reduction with its own guard.
    """
    before = _fitting_booking_ids(specialist)
    yield
    after = _fitting_booking_ids(specialist)

    stranded = before - after
    if stranded:
        raise ScheduleShrinkConflict(len(stranded))
