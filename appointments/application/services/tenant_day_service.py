"""The salon's day — a read-only projection over existing data (DRF-1063).

An administrator of the pilot salon currently sees nothing. Not "an empty
list": ``AppointmentViewSet.get_queryset`` filters to
``client=user`` or ``specialist__user=user`` and falls through to
``none()`` for everyone else, so the salon cannot learn who is coming
today except by asking the owner. This is the projection that answers
that question.

It is a projection and nothing more — ``Ayla MVP Appointment Contract``
§20: "Projection is not a Domain Entity and never becomes Source of
Truth." Nothing here writes, and every change to what it shows goes
through a command owned by the domain.

Three properties are deliberate:

*Times are rendered in the specialist's timezone, and labelled.*
DRF-1071 found the records list printing UTC — a client booked for 14:00
MSK read as 11:00. An operator planning a day must not be handed the same
trap, so every local field is suffixed ``_local`` and the tz name travels
with it. The UTC instants are kept alongside: a machine consumer should
not have to parse an offset back out.

*Masters with nothing booked are still listed.* A day journal that hides
idle masters cannot answer "who is free at three".

*No customer phone.* Owner decision DRF-1039: the salon reaches clients
through Ayla, not by being handed their number. The operational name is
included because greeting the right person is the point of the journal;
the number is not, and this surface must not become the way around that
rule.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date as date_cls, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone


@dataclass(frozen=True)
class DayInterval:
    """A local-time interval, e.g. working hours or a break."""
    start_local: str          # "HH:MM"
    end_local: str            # "HH:MM"


@dataclass(frozen=True)
class DayAbsence:
    """A time-off block overlapping the day."""
    id: str
    start_at: str
    end_at: str
    start_at_local: str
    end_at_local: str
    reason: str


@dataclass(frozen=True)
class DayBooking:
    appointment_id: str
    version: int
    status: str
    start_at: str
    end_at: str
    start_at_local: str
    end_at_local: str
    service_name: str
    duration_minutes: int | None
    price: str
    client_id: str
    client_name: str
    completed_by: str
    no_show_marked_by: str


@dataclass(frozen=True)
class DayMaster:
    specialist_id: str
    display_name: str
    status: str
    timezone_name: str
    is_working_day: bool
    # Which source shaped the day: "exception" when a one-off override
    # exists for this date, otherwise "weekly". Shown because "10:00-19:00"
    # from the template and "10:00-19:00" set by hand for today look
    # identical in a journal, and an administrator deciding whether to
    # change something needs to know which one they are looking at.
    schedule_source: str = "weekly"
    schedule_note: str = ""
    working_intervals: list[DayInterval] = field(default_factory=list)
    breaks: list[DayInterval] = field(default_factory=list)
    absences: list[DayAbsence] = field(default_factory=list)
    bookings: list[DayBooking] = field(default_factory=list)


@dataclass(frozen=True)
class DayClosure:
    """The whole salon closed, for the day or part of it."""
    id: str
    start_local: str | None      # "HH:MM"; None = from the start of the day
    end_local: str | None        # "HH:MM"; None = until the end of the day
    is_full_day: bool
    reason: str


@dataclass(frozen=True)
class TenantDay:
    date: str
    generated_at: str
    closures: list[DayClosure] = field(default_factory=list)
    masters: list[DayMaster] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _client_name(user) -> str:
    """The operational label for a customer — never their phone.

    Only the profile name fields. ``username`` is deliberately NOT a
    fallback, and that omission is the whole point of this function.

    Every ``username`` in this repository is machine-generated. There are
    six writers and not one of them stores a name a person chose:

    * ``users/services.py:125,293`` — ``bot:{channel}:{external_id}``
    * ``users/services.py:534`` — ``walkin:{uuid4hex}``
    * ``users/services.py:1061`` — ``user_{phone digits}``
    * ``users/social_auth.py:366`` — ``social_{provider}_{uid}``
    * ``users/views.py:303`` — ``anon_{short_id}``
    * ``users/management/commands/provision_salon_admin.py:69`` — the
      raw phone

    So the old ``username`` fallback could never produce a name, only an
    identifier — while two of those six forms **are** the customer's
    phone number. ``user_79991234567`` sailed straight through the
    previous ``startswith("+")`` guard, which put a full, unmasked phone
    into the salon's day journal and into the customer-lookup response
    whose own docstring (``tenants/appointments_api.py:298-301``)
    promises that "the phone is an input, never an output". That promise
    is owner decision DRF-1039, and this function was the way around it.

    Removed outright rather than blocklisted: a blocklist has to be
    re-audited every time somebody adds a seventh writer, and this
    guarantee has to hold by construction rather than by vigilance.

    An empty result is honest. Some clients arrive from the bot with
    nothing but an identifier, and inventing "Client #4" would make the
    journal look more certain than it is. Handing the salon a phone
    number it never asked for is not the better alternative.
    """
    if user is None:
        return ""
    return f"{user.first_name or ''} {user.last_name or ''}".strip()


def build_tenant_day(tenant, target_date: date_cls) -> TenantDay:
    """Assemble the day journal for one salon.

    Query budget is flat in the number of masters: one query each for
    salon closures, the roster, the weekly template, one-off schedule
    exceptions, absences and bookings. The per-master slicing below is
    done in Python over those result sets rather than by re-querying.
    """
    from appointments.models import (
        Appointment, SpecialistScheduleException, SpecialistTimeOff,
        SpecialistWorkingHours, TenantClosure,
    )
    from users.models import SpecialistProfile

    closures = [
        DayClosure(
            id=str(row.id),
            start_local=_hhmm(row.start_time),
            end_local=_hhmm(row.end_time),
            is_full_day=row.start_time is None and row.end_time is None,
            reason=row.reason or "",
        )
        for row in TenantClosure.objects
        .filter(tenant=tenant, date=target_date)
        .order_by("start_time")
    ]

    masters = list(
        SpecialistProfile.objects
        .filter(tenant=tenant)
        .select_related("user")
        .order_by("display_name", "id")
    )
    if not masters:
        return TenantDay(
            date=target_date.isoformat(),
            generated_at=timezone.now().isoformat(),
            closures=closures,
            masters=[],
            summary={
                "masters": 0,
                "bookings": 0,
                "by_status": {},
                "is_salon_closed": any(c.is_full_day for c in closures),
            },
        )

    # Each master's day is their own local calendar day. In practice a
    # salon shares one timezone, but nothing in the model guarantees it,
    # so the window is computed per master and the DB is queried once
    # over the union.
    windows: dict[str, tuple[datetime, datetime]] = {}
    for master in masters:
        tz = ZoneInfo(master.timezone)
        start_local = datetime.combine(target_date, time.min, tzinfo=tz)
        windows[str(master.id)] = (
            start_local, start_local + timedelta(days=1),
        )
    span_start = min(start for start, _ in windows.values())
    span_end = max(end for _, end in windows.values())

    master_ids = [m.id for m in masters]

    hours_by_master: dict[str, list[SpecialistWorkingHours]] = {}
    for row in SpecialistWorkingHours.objects.filter(
        specialist_id__in=master_ids,
        day_of_week=target_date.weekday(),
    ):
        hours_by_master.setdefault(str(row.specialist_id), []).append(row)

    # One-off overrides REPLACE the weekly template for this date — the
    # same precedence AvailabilityQueryService._get_working_hours applies
    # when it decides which slots to sell. A journal that showed the
    # template while the engine sold the exception would be worse than no
    # journal, because it would look authoritative.
    exception_by_master: dict[str, SpecialistScheduleException] = {
        str(row.specialist_id): row
        for row in SpecialistScheduleException.objects.filter(
            specialist_id__in=master_ids, date=target_date,
        )
    }

    absences_by_master: dict[str, list[SpecialistTimeOff]] = {}
    for row in SpecialistTimeOff.objects.filter(
        specialist_id__in=master_ids,
        start_at__lt=span_end,
        end_at__gt=span_start,
    ).order_by("start_at"):
        absences_by_master.setdefault(str(row.specialist_id), []).append(row)

    bookings_by_master: dict[str, list[Appointment]] = {}
    for row in (
        Appointment.objects
        .filter(
            tenant=tenant,
            specialist_id__in=master_ids,
            start_datetime__lt=span_end,
            end_datetime__gt=span_start,
        )
        .select_related("client", "service", "salon_service")
        .order_by("start_datetime")
    ):
        bookings_by_master.setdefault(str(row.specialist_id), []).append(row)

    day_masters: list[DayMaster] = []
    total = 0
    by_status: dict[str, int] = {}

    for master in masters:
        key = str(master.id)
        tz = ZoneInfo(master.timezone)
        window_start, window_end = windows[key]

        exception = exception_by_master.get(key)
        if exception is not None:
            frame_rows = [exception]
            schedule_source = "exception"
            schedule_note = exception.note or ""
        else:
            frame_rows = hours_by_master.get(key, [])
            schedule_source = "weekly"
            schedule_note = ""

        is_working_day = any(row.is_working_day for row in frame_rows)
        working_intervals = [
            DayInterval(
                start_local=_hhmm(row.start_time),
                end_local=_hhmm(row.end_time),
            )
            for row in frame_rows
            if row.is_working_day and row.start_time and row.end_time
        ]
        breaks = [
            DayInterval(
                start_local=_hhmm(row.break_start),
                end_local=_hhmm(row.break_end),
            )
            for row in frame_rows
            if row.is_working_day and row.break_start and row.break_end
        ]

        absences = [
            DayAbsence(
                id=str(row.id),
                start_at=row.start_at.isoformat(),
                end_at=row.end_at.isoformat(),
                start_at_local=row.start_at.astimezone(tz).isoformat(),
                end_at_local=row.end_at.astimezone(tz).isoformat(),
                reason=row.reason or "",
            )
            for row in absences_by_master.get(key, [])
        ]

        bookings = []
        for row in bookings_by_master.get(key, []):
            # The union window above is wider than this master's own day
            # whenever the salon spans timezones — re-check per master so
            # a booking never shows up on the wrong date.
            if not (
                row.start_datetime < window_end
                and row.end_datetime > window_start
            ):
                continue
            bookings.append(DayBooking(
                appointment_id=str(row.id),
                version=row.version,
                status=row.status,
                start_at=row.start_datetime.isoformat(),
                end_at=row.end_datetime.isoformat(),
                start_at_local=row.start_datetime.astimezone(tz).isoformat(),
                end_at_local=row.end_datetime.astimezone(tz).isoformat(),
                service_name=row.snapshot_service_name or "",
                duration_minutes=row.snapshot_duration_minutes,
                price=str(
                    row.snapshot_price
                    if row.snapshot_price is not None else row.price
                ),
                client_id=str(row.client_id),
                client_name=_client_name(row.client),
                completed_by=row.completed_by,
                no_show_marked_by=row.no_show_marked_by,
            ))
            total += 1
            by_status[row.status] = by_status.get(row.status, 0) + 1

        day_masters.append(DayMaster(
            specialist_id=key,
            display_name=master.display_name,
            status=master.status,
            timezone_name=master.timezone,
            is_working_day=is_working_day,
            schedule_source=schedule_source,
            schedule_note=schedule_note,
            working_intervals=working_intervals,
            breaks=breaks,
            absences=absences,
            bookings=bookings,
        ))

    return TenantDay(
        date=target_date.isoformat(),
        generated_at=timezone.now().isoformat(),
        closures=closures,
        masters=day_masters,
        summary={
            "masters": len(day_masters),
            "bookings": total,
            "by_status": by_status,
            "is_salon_closed": any(c.is_full_day for c in closures),
        },
    )
