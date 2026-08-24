"""Internal Bearer surface for a specialist's schedule (DRF-1062, DRF-1126).

Two operations, one authorisation story: block a specialist's time
(DRF-1062, POST) and read the frame of their working days (DRF-1126,
GET).

Why this exists. The bot owns the *interface* through which a master asks
for a day off — it lives in the Mini App and it works. What it does not
own is the schedule: once the customer picker reads slots from Ayla, an
approval written into the bot's own ``apps.scheduling`` changes nothing a
client can see. The administrator would approve a day off, be told it
succeeded, and the day would stay on sale. That silent disagreement
between two stores is the defect DRF-1062 exists to remove, so the
approval has to land here instead.

The same sentence, one screen over. DRF-1126: the master's own schedule
screen builds its days from the bot's local ``apps.scheduling``
``WorkingHours``, which no longer has a writer that syncs from Ayla — the
seeder that manufactured the pilot's 10:00-19:00 stub was removed by
DRF-1062 and nothing replaced it. So the salon edits the graph on its own
surface, the customer's picker (reading Ayla since PR #1186) shows the new
hours, and the master's screen shows the old ones. Neither side reports an
error and both look right. The bot cannot fix that alone: its Ayla client
has ``get_available_times`` (bookable slots for one service on one day)
and ``create_specialist_time_off``, and nothing that answers "what is this
master's working frame". This GET is that missing read.

Why not the salon-admin surface. ``/api/v1/tenants/...`` requires a staff
JWT and a tenant resolved by ``TenantContextMiddleware``; the bot has
neither, and ``/api/v1/internal/*`` is excluded from that middleware
(``users/middleware.py:225``), so ``request.tenant`` there is always
``None``. Rather than invent an authorisation path, this route extends
the one the bot already uses to read slots.

Security shape (main-window conditions, 2026-08-15):

* the ``tenant_id`` in the body is a claim, not a credential — the
  specialist must actually belong to it, or the answer is **404**, never
  403. A 403 would confirm the specialist exists to anyone who guessed a
  UUID (adjacent to DRF-1036);
* the response carries no personal data at all — it reports the block it
  wrote and nothing about the people affected by it.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_date
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer,
)
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import SpecialistTimeOff

from .permissions import IsInternalBearer
from .response import error_response, success_response
from .schedule_api import (
    _WORKING_HOURS_RESPONSE_FIELDS, _invalidate_slots, _to_to_dict,
    _wh_to_dict,
)

logger = logging.getLogger(__name__)


class InternalTimeOffCreateSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField(
        help_text="Tenant the specialist must belong to. Verified, not trusted.",
    )
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, data):
        if data["end_at"] <= data["start_at"]:
            raise serializers.ValidationError("end_at must be after start_at.")
        return data


_TIME_OFF_RESPONSE_FIELDS = {
    "id": serializers.UUIDField(),
    "specialist_id": serializers.UUIDField(),
    "start_at": serializers.DateTimeField(),
    "end_at": serializers.DateTimeField(),
    "reason": serializers.CharField(allow_blank=True),
}


class InternalSpecialistTimeOffView(APIView):
    """POST /api/v1/internal/specialists/{specialist_id}/time-off/

    Blocks a specialist's time on behalf of a salon administrator acting
    through the bot's Mini App.

    Refuses with 409 ``HAS_ACTIVE_APPOINTMENTS`` when live bookings sit in
    the period — the same rule the human-facing surfaces enforce. The
    flow that settles those bookings (preview → decide per booking →
    apply) is deliberately NOT exposed here: cancelling somebody's
    appointment is a decision with money and a message attached, and it
    belongs on a surface where a named human is the actor, not a shared
    service token.
    """

    # DRF's JWTAuthentication would 401 the non-JWT bearer before
    # IsInternalBearer ever runs (same rationale as masters_internal_api).
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = InternalTimeOffCreateSerializer

    @extend_schema(
        tags=["internal"],
        request=InternalTimeOffCreateSerializer,
        responses={
            201: inline_serializer(
                name="InternalTimeOffCreateResponse",
                fields=_TIME_OFF_RESPONSE_FIELDS,
            ),
            404: OpenApiResponse(
                description="Specialist not found in the given tenant",
            ),
            409: OpenApiResponse(
                description="Active appointments overlap this period",
            ),
        },
    )
    def post(self, request: Request, specialist_id) -> Response:
        from users.models import SpecialistProfile

        serializer = InternalTimeOffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # The ownership check. Filtering on BOTH ids in one query means a
        # specialist that exists in another tenant is indistinguishable
        # from one that does not exist at all.
        specialist = (
            SpecialistProfile.objects
            .filter(id=specialist_id, tenant_id=data["tenant_id"])
            .first()
        )
        if specialist is None:
            logger.info(
                "internal.time_off.not_found specialist=%s tenant=%s",
                specialist_id, data["tenant_id"],
            )
            return error_response(
                "NOT_FOUND", "Specialist not found.", status_code=404,
            )

        start_at, end_at = data["start_at"], data["end_at"]

        from appointments.application.services.schedule_impact_service import (
            count_active_bookings_in_window,
        )
        active_count = count_active_bookings_in_window(
            specialist, start_at, end_at,
        )
        if active_count > 0:
            return error_response(
                "HAS_ACTIVE_APPOINTMENTS",
                f"Cannot block time: {active_count} active appointment(s) "
                "overlap this period.",
                status_code=409,
            )

        time_off = SpecialistTimeOff.objects.create(
            specialist=specialist,
            start_at=start_at,
            end_at=end_at,
            reason=data.get("reason", ""),
        )

        _invalidate_slots(specialist.id, start_at.date(), end_at.date())

        logger.info(
            "internal.time_off.created id=%s specialist=%s tenant=%s",
            time_off.id, specialist.id, data["tenant_id"],
        )

        # No personal data: the block that was written, nothing about who
        # it affects (DRF-1036 adjacency).
        return success_response(
            {
                "id": str(time_off.id),
                "specialist_id": str(specialist.id),
                "start_at": time_off.start_at.isoformat(),
                "end_at": time_off.end_at.isoformat(),
                "reason": time_off.reason,
            },
            status_code=201,
        )


_SCHEDULE_READ_MAX_SPAN_DAYS = 60


_EXCEPTION_RESPONSE_FIELDS = {
    "id": serializers.UUIDField(),
    "date": serializers.CharField(),
    "is_working_day": serializers.BooleanField(),
    "start_time": serializers.CharField(allow_null=True),
    "end_time": serializers.CharField(allow_null=True),
    "break_start": serializers.CharField(allow_null=True),
    "break_end": serializers.CharField(allow_null=True),
    "note": serializers.CharField(allow_blank=True),
}


def _exception_to_dict(row) -> dict:
    """Same shape the salon-admin surface returns.

    Deliberately a copy of ``schedule_admin_api._exception_to_dict``
    rather than an import of it: that module pulls in the whole absence
    resolution / impact-preview stack, and this file's entire security
    argument is that it stays small enough to read in one sitting. The
    two are pinned equal by a test, so a change to either is caught.
    """
    def fmt(value):
        return value.strftime("%H:%M") if value else None

    return {
        "id": str(row.id),
        "date": row.date.isoformat(),
        "is_working_day": row.is_working_day,
        "start_time": fmt(row.start_time),
        "end_time": fmt(row.end_time),
        "break_start": fmt(row.break_start),
        "break_end": fmt(row.break_end),
        "note": row.note,
    }


class InternalSpecialistScheduleView(APIView):
    """GET /api/v1/internal/specialists/{specialist_id}/schedule/

    The working frame of one specialist over a bounded date range, for
    the bot's master-facing screens (DRF-1126).

    Query: ``tenant_id`` (required), ``from`` / ``to`` (optional ISO
    dates; default today .. today+13).

    **Three sources in one call, because a day needs all three.** The
    caller resolves a date the way the booking engine does: a one-off
    ``exceptions`` entry replaces the weekly frame for that date, and
    ``time_off`` subtracts from whichever frame won. Splitting them over
    three round trips would invite a consumer to draw a day from the
    weekly template alone — which is the class of bug this endpoint
    exists to remove, one layer down.

    **The frame, never the bookings.** Who is coming and when is the
    other half of the master's screen and it already has a source: the
    ``RemoteBookingProxy`` mirror the Ayla event stream writes, which
    DRF-1085 pointed that screen at. Serving appointments here would
    create a second answer to a question that already has one, and would
    put customers' data behind a shared service token. There is no
    personal data in this response at all.

    Security shape is the POST's above, unchanged: ``tenant_id`` is a
    claim and not a credential, and a specialist belonging to another
    tenant is answered **404**, indistinguishable from one that does not
    exist. A 403 would confirm a guessed UUID (DRF-1036 adjacency).

    A separate view rather than a ``get`` on the class above, so that a
    future ``post`` here cannot inherit a read grant and the two
    operations keep their own permission lists.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    DEFAULT_SPAN_DAYS = 13

    @extend_schema(
        tags=["internal"],
        parameters=[
            OpenApiParameter(
                name="tenant_id", required=True, type=str,
                description=(
                    "Tenant the specialist must belong to. Verified, "
                    "not trusted."
                ),
            ),
            OpenApiParameter(
                name="from", required=False, type=str,
                description="ISO date, inclusive. Defaults to today.",
            ),
            OpenApiParameter(
                name="to", required=False, type=str,
                description=(
                    "ISO date, inclusive. Defaults to `from` plus 13 "
                    "days, and may be at most 60 days after `from`."
                ),
            ),
        ],
        responses={
            200: inline_serializer(
                name="InternalSpecialistScheduleResponse",
                fields={
                    "specialist_id": serializers.UUIDField(),
                    "tenant_id": serializers.UUIDField(),
                    "timezone": serializers.CharField(),
                    "from": serializers.CharField(),
                    "to": serializers.CharField(),
                    "weekly": inline_serializer(
                        name="InternalScheduleWeeklyDay",
                        fields=_WORKING_HOURS_RESPONSE_FIELDS,
                        many=True,
                    ),
                    "exceptions": inline_serializer(
                        name="InternalScheduleException",
                        fields=_EXCEPTION_RESPONSE_FIELDS,
                        many=True,
                    ),
                    "time_off": inline_serializer(
                        name="InternalScheduleTimeOff",
                        fields=_TIME_OFF_RESPONSE_FIELDS,
                        many=True,
                    ),
                },
            ),
            400: OpenApiResponse(description="Bad tenant_id or date range"),
            404: OpenApiResponse(
                description="Specialist not found in the given tenant",
            ),
        },
    )
    def get(self, request: Request, specialist_id) -> Response:
        from appointments.models import (
            SpecialistScheduleException, SpecialistTimeOff,
            SpecialistWorkingHours,
        )
        from users.models import SpecialistProfile

        raw_tenant = (request.query_params.get("tenant_id") or "").strip()
        if not raw_tenant:
            return error_response(
                "VALIDATION_ERROR",
                "Query parameter tenant_id is required.",
                status_code=400,
            )
        try:
            tenant_id = UUID(raw_tenant)
        except (ValueError, AttributeError, TypeError):
            return error_response(
                "VALIDATION_ERROR",
                "Query parameter tenant_id must be a UUID.",
                status_code=400,
            )

        window = self._parse_window(request)
        if isinstance(window, Response):
            return window
        from_date, to_date = window

        # The ownership check, byte-for-byte the POST's: filtering on
        # BOTH ids in one query makes "exists elsewhere" and "does not
        # exist" the same answer.
        specialist = (
            SpecialistProfile.objects
            .filter(id=specialist_id, tenant_id=tenant_id)
            .first()
        )
        if specialist is None:
            logger.info(
                "internal.schedule.not_found specialist=%s tenant=%s",
                specialist_id, tenant_id,
            )
            return error_response(
                "NOT_FOUND", "Specialist not found.", status_code=404,
            )

        # All seven days, always. A missing row means "not working", and
        # a consumer handed a five-element list has to infer that — the
        # exact guesswork this endpoint exists to stop. Same fill as
        # ScheduleView.get, so the bot's master screen and the master's
        # own pro-app tab cannot disagree about an unset weekday.
        existing = {
            wh.day_of_week: wh
            for wh in SpecialistWorkingHours.objects.filter(
                specialist=specialist,
            )
        }
        day_names = dict(SpecialistWorkingHours.DayOfWeek.choices)
        weekly = [
            _wh_to_dict(existing[day]) if day in existing else {
                "day_of_week": day,
                "day_name": day_names[day],
                "is_working_day": False,
                "start_time": None,
                "end_time": None,
                "break_start": None,
                "break_end": None,
            }
            for day in range(7)
        ]

        exceptions = [
            _exception_to_dict(row)
            for row in SpecialistScheduleException.objects.filter(
                specialist=specialist,
                date__gte=from_date,
                date__lte=to_date,
            ).order_by("date")
        ]

        # Overlap, not containment: an absence that began yesterday and
        # ends tomorrow blocks today, and a consumer sent only the blocks
        # *starting* inside the window would draw today as free.
        #
        # The bounds are built from two dates in the specialist's own
        # timezone rather than from an instant plus an offset — the same
        # construction TenantClosureBusyIntervalProvider uses, and the
        # reason is DST: on a transition day the second form silently
        # covers 23 or 25 hours.
        tz = self._zone_for(specialist)
        window_start = datetime.combine(from_date, time.min, tzinfo=tz)
        window_end = datetime.combine(
            to_date + timedelta(days=1), time.min, tzinfo=tz,
        )
        time_off = [
            _to_to_dict(row)
            for row in SpecialistTimeOff.objects.filter(
                specialist=specialist,
                start_at__lt=window_end,
                end_at__gt=window_start,
            ).order_by("start_at")
        ]

        logger.info(
            "internal.schedule.read specialist=%s tenant=%s from=%s to=%s "
            "exceptions=%d time_off=%d",
            specialist.id, tenant_id, from_date, to_date,
            len(exceptions), len(time_off),
        )

        return success_response({
            "specialist_id": str(specialist.id),
            "tenant_id": str(tenant_id),
            "timezone": specialist.timezone or settings.TIME_ZONE,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "weekly": weekly,
            "exceptions": exceptions,
            "time_off": time_off,
        })

    @staticmethod
    def _zone_for(specialist) -> ZoneInfo:
        try:
            return ZoneInfo(specialist.timezone or settings.TIME_ZONE)
        except Exception:  # noqa: BLE001 — a bad tz string must not 500
            logger.warning(
                "internal.schedule.bad_timezone specialist=%s value=%r",
                specialist.id, specialist.timezone,
            )
            return ZoneInfo(settings.TIME_ZONE)

    def _parse_window(self, request: Request):
        """``(from_date, to_date)``, or an error ``Response``.

        The span is capped rather than paginated. A schedule read is a
        frame and not a feed: the caller wants the fortnight it is
        drawing, and an uncapped range behind a shared service token is
        an easy way to make the database do unbounded work on request.
        """
        raw_from = (request.query_params.get("from") or "").strip()
        if raw_from:
            from_date = parse_date(raw_from)
            if from_date is None:
                return error_response(
                    "VALIDATION_ERROR",
                    "Parameter `from` must be a date in YYYY-MM-DD form.",
                    status_code=400,
                )
        else:
            from_date = django_timezone.localdate()

        raw_to = (request.query_params.get("to") or "").strip()
        if raw_to:
            to_date = parse_date(raw_to)
            if to_date is None:
                return error_response(
                    "VALIDATION_ERROR",
                    "Parameter `to` must be a date in YYYY-MM-DD form.",
                    status_code=400,
                )
        else:
            to_date = from_date + timedelta(days=self.DEFAULT_SPAN_DAYS)

        if to_date < from_date:
            return error_response(
                "VALIDATION_ERROR",
                "Parameter `to` must not be earlier than `from`.",
                status_code=400,
            )
        if (to_date - from_date).days > _SCHEDULE_READ_MAX_SPAN_DAYS:
            return error_response(
                "VALIDATION_ERROR",
                "Range must not exceed "
                f"{_SCHEDULE_READ_MAX_SPAN_DAYS} days.",
                status_code=400,
            )
        return from_date, to_date
