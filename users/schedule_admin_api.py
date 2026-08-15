"""Salon-admin schedule surface — any master of your own tenant (DRF-1062).

The pro-app surface in ``schedule_api`` binds every operation to
``request.user.specialist_profile``: the route carries no master id, so an
administrator has nowhere to put one. That is why salons cannot manage
schedules today — and why the pilot's four masters still sit on a
10:00–19:00 seven-days-a-week stub while none of them can even log in
(they have no phone numbers, and login is OTP).

This module is the second entry point, not a second implementation. Every
view subclasses its pro-app counterpart and overrides exactly two things:

* ``_get_specialist`` — resolve the master from the URL instead of the
  session, scoped to ``request.tenant``;
* ``permission_classes`` — salon admin or Ayla platform staff.

Validation, the 409 on active appointments, and slot-cache invalidation
are inherited unchanged, so the two surfaces cannot drift apart.

Tenant scoping follows the precedent set by the only other
``IsTenantAdmin`` consumer (``tenants/relationships_admin_api.py``): the
tenant comes from middleware, never from the body, and a master in another
salon is reported as 404 rather than 403 so the surface does not leak which
ids exist.
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import SpecialistScheduleException, TenantClosure

from .permissions import IsProApp, IsTenantAdminOrPlatformAdmin
from .response import error_response, success_response
from .schedule_api import ScheduleView, TimeOffDetailView, TimeOffListView

logger = logging.getLogger(__name__)

_ADMIN_PERMISSIONS = [
    permissions.IsAuthenticated,
    IsProApp,
    IsTenantAdminOrPlatformAdmin,
]


class _TenantScopedSpecialistMixin:
    """Resolve the target master from the URL, scoped to the request tenant."""

    permission_classes = _ADMIN_PERMISSIONS

    def _get_specialist(self, request: Request):
        from users.models import SpecialistProfile

        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            return None

        # tenant is nullable on SpecialistProfile; filtering by the
        # resolved tenant object (never a body value) means a master with
        # tenant NULL, or one belonging to another salon, is simply not
        # found — the inherited views turn that into 404 NOT_FOUND.
        return (
            SpecialistProfile.objects
            .filter(
                id=self.kwargs.get("specialist_id"),
                tenant=request_tenant,
            )
            .first()
        )


# ---------------------------------------------------------------------------
# Weekly template + time-off — inherited behaviour, admin entry point
# ---------------------------------------------------------------------------

class AdminScheduleView(_TenantScopedSpecialistMixin, ScheduleView):
    """GET/PUT/PATCH /api/v1/tenants/me/masters/{specialist_id}/schedule/"""

    def get(self, request: Request, **kwargs) -> Response:
        return super().get(request)

    def put(self, request: Request, **kwargs) -> Response:
        return super().put(request)

    def patch(self, request: Request, **kwargs) -> Response:
        return super().patch(request)


class ResolutionSerializer(serializers.Serializer):
    appointment_id = serializers.UUIDField()
    action = serializers.ChoiceField(choices=["cancel"])


class AbsenceWithResolutionsSerializer(serializers.Serializer):
    """An absence plus what happens to everyone booked into it.

    ``impact_token`` is required, not optional: without it the caller
    would be confirming decisions against a set of bookings it never
    actually saw.
    """

    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    impact_token = serializers.CharField()
    resolutions = ResolutionSerializer(many=True)

    def validate(self, data):
        if data["end_at"] <= data["start_at"]:
            raise serializers.ValidationError("end_at must be after start_at.")
        seen = {str(item["appointment_id"]) for item in data["resolutions"]}
        if len(seen) != len(data["resolutions"]):
            raise serializers.ValidationError(
                "Duplicate appointment_id in resolutions."
            )
        return data


def _impact_to_dict(impact) -> dict:
    return {
        "specialist_id": impact.specialist_id,
        "start_at": impact.start_at,
        "end_at": impact.end_at,
        "timezone": impact.timezone_name,
        "impact_token": impact.impact_token,
        "bookings": [
            {
                "appointment_id": b.appointment_id,
                "version": b.version,
                "status": b.status,
                "start_at_local": b.start_at_local,
                "end_at_local": b.end_at_local,
                "service_name": b.service_name,
                "duration_minutes": b.duration_minutes,
                "price": b.price,
                "payment_status": b.payment_status,
                "refund_percent_if_cancelled": b.refund_percent_if_cancelled,
            }
            for b in impact.bookings
        ],
    }


def _parse_window(request: Request):
    """Read start_at/end_at from the query string. Returns (start, end, error)."""
    from django.utils.dateparse import parse_datetime

    raw_start = request.query_params.get("start_at")
    raw_end = request.query_params.get("end_at")
    if not raw_start or not raw_end:
        return None, None, error_response(
            "MISSING_PARAM", "start_at and end_at are required.", status_code=400,
        )

    def _parse(raw: str):
        parsed = parse_datetime(raw)
        if parsed is not None:
            return parsed
        # "2026-08-18T09:00:00+03:00" arrives as "...09:00:00 03:00" when
        # the caller forgot to percent-encode the '+' — the single most
        # common way to get an unreadable 400 out of a timestamp that
        # looks perfectly valid in the logs. Accept it rather than make
        # every caller rediscover this.
        return parse_datetime(raw.replace(" ", "+", 1))

    start_at, end_at = _parse(raw_start), _parse(raw_end)
    if start_at is None or end_at is None:
        return None, None, error_response(
            "INVALID_PARAM", "start_at and end_at must be ISO-8601.", status_code=400,
        )
    if end_at <= start_at:
        return None, None, error_response(
            "INVALID_PARAM", "end_at must be after start_at.", status_code=400,
        )
    return start_at, end_at, None


class AdminScheduleImpactView(_TenantScopedSpecialistMixin, APIView):
    """GET .../masters/{specialist_id}/schedule/impact/?start_at=&end_at=

    Which live bookings a proposed absence would displace. Read-only —
    nothing is blocked and nothing is cancelled by looking.

    The response carries an ``impact_token`` fingerprinting the set; the
    write side rejects a stale one so a booking made while the
    administrator was deciding cannot be silently swept up or missed.
    """

    permission_classes = _ADMIN_PERMISSIONS

    @extend_schema(
        responses={
            200: OpenApiResponse(description="Affected bookings + impact_token"),
            400: OpenApiResponse(description="Bad or missing window"),
            404: OpenApiResponse(description="Specialist not found in this tenant"),
        },
    )
    def get(self, request: Request, **kwargs) -> Response:
        from appointments.application.services.schedule_impact_service import (
            get_schedule_impact,
        )

        specialist = self._get_specialist(request)
        if not specialist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        start_at, end_at, error = _parse_window(request)
        if error is not None:
            return error

        impact = get_schedule_impact(specialist, start_at, end_at)
        return success_response(_impact_to_dict(impact))


class AdminTimeOffListView(_TenantScopedSpecialistMixin, TimeOffListView):
    """GET/POST /api/v1/tenants/me/masters/{specialist_id}/time-off/

    Two shapes of POST:

    * without ``resolutions`` — inherited behaviour, including the 409
      HAS_ACTIVE_APPOINTMENTS refusal when live bookings sit in the
      period. That refusal is right on its own: nobody should be able to
      strand a booked client by accident.
    * with ``resolutions`` — DRF-1062 §C. The absence and a decision for
      every displaced booking are applied together, in one transaction.
      Either the salon closes the time and settles with the people booked
      into it, or nothing happens.
    """

    def get(self, request: Request, **kwargs) -> Response:
        return super().get(request)

    def post(self, request: Request, **kwargs) -> Response:
        if "resolutions" not in request.data:
            return super().post(request)
        return self._post_with_resolutions(request)

    def _post_with_resolutions(self, request: Request) -> Response:
        from users.services import (
            ImpactChangedError,
            UnresolvedBookingsError,
            UnsupportedResolutionError,
            apply_absence_with_resolutions,
        )

        specialist = self._get_specialist(request)
        if not specialist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        serializer = AbsenceWithResolutionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            summary = apply_absence_with_resolutions(
                specialist=specialist,
                start_at=data["start_at"],
                end_at=data["end_at"],
                reason=data.get("reason", ""),
                resolutions=data["resolutions"],
                impact_token=data["impact_token"],
                actor=request.user,
            )
        except ImpactChangedError as exc:
            # 409 with a fresh preview: the administrator decided against
            # a set of bookings that no longer matches reality.
            return error_response(
                "IMPACT_CHANGED",
                "The affected bookings changed. Review them and confirm again.",
                status_code=409,
                details=_impact_to_dict(exc.impact),
            )
        except UnresolvedBookingsError as exc:
            return error_response(
                "HAS_ACTIVE_APPOINTMENTS",
                "Every affected booking needs a decision.",
                status_code=409,
                details={
                    "unresolved": exc.missing,
                    **_impact_to_dict(exc.impact),
                },
            )
        except UnsupportedResolutionError as exc:
            return error_response("UNSUPPORTED_RESOLUTION", str(exc), status_code=400)

        logger.info(
            "schedule.absence_with_resolutions actor=%s tenant=%s specialist=%s",
            request.user.pk, request.tenant.pk, specialist.pk,
        )
        return success_response(summary, status_code=201)


class AdminTimeOffDetailView(_TenantScopedSpecialistMixin, TimeOffDetailView):
    """DELETE /api/v1/tenants/me/masters/{specialist_id}/time-off/{pk}/"""

    def delete(self, request: Request, **kwargs) -> Response:
        return super().delete(request, pk=kwargs["pk"])


# ---------------------------------------------------------------------------
# Per-date schedule exceptions (new in DRF-1062)
# ---------------------------------------------------------------------------

class ScheduleExceptionSerializer(serializers.Serializer):
    """Mirrors WorkingHoursSerializer so a date override and the weekly
    template reject exactly the same shapes."""

    date = serializers.DateField()
    is_working_day = serializers.BooleanField()
    start_time = serializers.TimeField(allow_null=True, required=False, default=None)
    end_time = serializers.TimeField(allow_null=True, required=False, default=None)
    break_start = serializers.TimeField(allow_null=True, required=False, default=None)
    break_end = serializers.TimeField(allow_null=True, required=False, default=None)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, data):
        if not data.get("is_working_day"):
            for field in ("start_time", "end_time", "break_start", "break_end"):
                if data.get(field):
                    raise serializers.ValidationError(
                        "A non-working exception must not carry times."
                    )
            return data

        start, end = data.get("start_time"), data.get("end_time")
        if not start or not end:
            raise serializers.ValidationError(
                "start_time and end_time are required for working days."
            )
        if start >= end:
            raise serializers.ValidationError("start_time must be before end_time.")

        break_start, break_end = data.get("break_start"), data.get("break_end")
        if break_start or break_end:
            if not (break_start and break_end):
                raise serializers.ValidationError(
                    "Both break_start and break_end must be provided together."
                )
            if break_start >= break_end:
                raise serializers.ValidationError(
                    "break_start must be before break_end."
                )
            if break_start < start or break_end > end:
                raise serializers.ValidationError(
                    "Break must be within working hours."
                )
        return data


_EXCEPTION_RESPONSE_FIELDS = {
    "id": serializers.UUIDField(),
    "date": serializers.DateField(),
    "is_working_day": serializers.BooleanField(),
    "start_time": serializers.CharField(allow_null=True),
    "end_time": serializers.CharField(allow_null=True),
    "break_start": serializers.CharField(allow_null=True),
    "break_end": serializers.CharField(allow_null=True),
    "note": serializers.CharField(allow_blank=True),
}


def _exception_to_dict(row: SpecialistScheduleException) -> dict:
    fmt = lambda t: t.strftime("%H:%M") if t else None  # noqa: E731
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


class AdminScheduleExceptionListView(_TenantScopedSpecialistMixin, APIView):
    """GET/PUT /api/v1/tenants/me/masters/{specialist_id}/schedule-exceptions/

    PUT rather than POST: one row per (master, date) by constraint, so
    setting an override twice for the same date must replace it, not fail.
    """

    permission_classes = _ADMIN_PERMISSIONS
    serializer_class = ScheduleExceptionSerializer

    @extend_schema(
        responses={
            200: inline_serializer(
                name="ScheduleExceptionListItem",
                fields=_EXCEPTION_RESPONSE_FIELDS,
                many=True,
            ),
            404: OpenApiResponse(description="Specialist not found in this tenant"),
        },
    )
    def get(self, request: Request, **kwargs) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        qs = SpecialistScheduleException.objects.filter(specialist=specialist)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)

        return success_response([_exception_to_dict(row) for row in qs])

    @extend_schema(
        request=ScheduleExceptionSerializer,
        responses={
            200: inline_serializer(
                name="ScheduleExceptionUpsertResponse",
                fields=_EXCEPTION_RESPONSE_FIELDS,
            ),
            404: OpenApiResponse(description="Specialist not found in this tenant"),
        },
    )
    def put(self, request: Request, **kwargs) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        serializer = ScheduleExceptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        row, _ = SpecialistScheduleException.objects.update_or_create(
            specialist=specialist,
            date=data["date"],
            defaults={
                "is_working_day": data["is_working_day"],
                "start_time": data.get("start_time"),
                "end_time": data.get("end_time"),
                "break_start": data.get("break_start"),
                "break_end": data.get("break_end"),
                "note": data.get("note", ""),
            },
        )
        logger.info(
            "schedule.exception_set actor=%s tenant=%s specialist=%s date=%s working=%s",
            request.user.pk, request.tenant.pk, specialist.pk,
            data["date"], data["is_working_day"],
        )
        return success_response(_exception_to_dict(row))


class AdminScheduleExceptionDetailView(_TenantScopedSpecialistMixin, APIView):
    """DELETE .../masters/{specialist_id}/schedule-exceptions/{date}/ —
    drop the override and fall back to the weekly template."""

    permission_classes = _ADMIN_PERMISSIONS
    serializer_class = ScheduleExceptionSerializer

    @extend_schema(request=None, responses={204: None, 404: OpenApiResponse()})
    def delete(self, request: Request, **kwargs) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response("NOT_FOUND", "Specialist not found.", status_code=404)

        try:
            row = SpecialistScheduleException.objects.get(
                specialist=specialist, date=kwargs["date"],
            )
        except SpecialistScheduleException.DoesNotExist:
            return error_response("NOT_FOUND", "Exception not found.", status_code=404)

        row.delete()  # post_delete signal invalidates the slot cache
        return Response(status=204)


# ---------------------------------------------------------------------------
# Salon-wide closures (new in DRF-1062)
# ---------------------------------------------------------------------------

class TenantClosureSerializer(serializers.Serializer):
    date = serializers.DateField()
    start_time = serializers.TimeField(allow_null=True, required=False, default=None)
    end_time = serializers.TimeField(allow_null=True, required=False, default=None)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, data):
        start, end = data.get("start_time"), data.get("end_time")
        if bool(start) != bool(end):
            raise serializers.ValidationError(
                "Provide both start_time and end_time, or neither for a full day."
            )
        if start and end and start >= end:
            raise serializers.ValidationError("start_time must be before end_time.")
        return data


_CLOSURE_RESPONSE_FIELDS = {
    "id": serializers.UUIDField(),
    "date": serializers.DateField(),
    "start_time": serializers.CharField(allow_null=True),
    "end_time": serializers.CharField(allow_null=True),
    "reason": serializers.CharField(allow_blank=True),
}


def _closure_to_dict(row: TenantClosure) -> dict:
    fmt = lambda t: t.strftime("%H:%M") if t else None  # noqa: E731
    return {
        "id": str(row.id),
        "date": row.date.isoformat(),
        "start_time": fmt(row.start_time),
        "end_time": fmt(row.end_time),
        "reason": row.reason,
    }


class TenantClosureListView(APIView):
    """GET/POST /api/v1/tenants/me/closures/ — close the whole salon.

    One row closes the salon for every master it has, now and later. The
    alternative — writing a time-off row per master — would turn one
    decision into N rows of derived data that drift apart the moment the
    roster changes.
    """

    permission_classes = _ADMIN_PERMISSIONS
    serializer_class = TenantClosureSerializer

    @extend_schema(
        responses={
            200: inline_serializer(
                name="TenantClosureListItem",
                fields=_CLOSURE_RESPONSE_FIELDS,
                many=True,
            ),
        },
    )
    def get(self, request: Request) -> Response:
        qs = TenantClosure.objects.filter(tenant=request.tenant)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        return success_response([_closure_to_dict(row) for row in qs])

    @extend_schema(
        request=TenantClosureSerializer,
        responses={
            201: inline_serializer(
                name="TenantClosureCreateResponse",
                fields=_CLOSURE_RESPONSE_FIELDS,
            ),
            409: OpenApiResponse(description="Closure already set for this date"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = TenantClosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from django.db import IntegrityError
        try:
            row = TenantClosure.objects.create(
                tenant=request.tenant,
                date=data["date"],
                start_time=data.get("start_time"),
                end_time=data.get("end_time"),
                reason=data.get("reason", ""),
            )
        except IntegrityError:
            return error_response(
                "CLOSURE_EXISTS",
                "A closure already covers this date.",
                status_code=409,
            )

        logger.info(
            "schedule.closure_set actor=%s tenant=%s date=%s full_day=%s",
            request.user.pk, request.tenant.pk, data["date"], row.is_full_day,
        )
        return success_response(_closure_to_dict(row), status_code=201)


class TenantClosureDetailView(APIView):
    """DELETE /api/v1/tenants/me/closures/{pk}/ — reopen the salon."""

    permission_classes = _ADMIN_PERMISSIONS
    serializer_class = TenantClosureSerializer

    @extend_schema(request=None, responses={204: None, 404: OpenApiResponse()})
    def delete(self, request: Request, pk) -> Response:
        try:
            row = TenantClosure.objects.get(id=pk, tenant=request.tenant)
        except TenantClosure.DoesNotExist:
            return error_response("NOT_FOUND", "Closure not found.", status_code=404)

        row.delete()  # post_delete signal invalidates the slot cache
        return Response(status=204)
