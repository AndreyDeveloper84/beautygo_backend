"""
Schedule and TimeOff management APIs for specialists (Pro App).

DRF-131: Working Hours CRUD — GET/PUT/PATCH /api/v1/specialists/me/schedule/
DRF-132: TimeOff/Blocks CRUD — GET/POST /api/v1/specialists/me/time-off/
                                DELETE    /api/v1/specialists/me/time-off/{id}/
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES
from appointments.models import SpecialistTimeOff, SpecialistWorkingHours

from .permissions import IsSpecialist
from .response import error_response, success_response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache invalidation helper
# ---------------------------------------------------------------------------

def _invalidate_slots(specialist_id, date_from: date, date_to: date) -> None:
    """Invalidate slot cache for a specialist across a date range (best-effort)."""
    try:
        from appointments.infrastructure.cache.slot_cache import SlotCacheService
        cache_svc = SlotCacheService()
        current = date_from
        while current <= date_to:
            cache_svc.invalidate(specialist_id, current)
            current += timedelta(days=1)
    except Exception:
        logger.warning(
            "Failed to invalidate slot cache for specialist %s", specialist_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class WorkingHoursSerializer(serializers.Serializer):
    """Single day working hours entry."""
    day_of_week = serializers.IntegerField(min_value=0, max_value=6)
    is_working_day = serializers.BooleanField()
    start_time = serializers.TimeField(allow_null=True, required=False, default=None)
    end_time = serializers.TimeField(allow_null=True, required=False, default=None)
    break_start = serializers.TimeField(allow_null=True, required=False, default=None)
    break_end = serializers.TimeField(allow_null=True, required=False, default=None)

    def validate(self, data):
        if data.get('is_working_day'):
            start = data.get('start_time')
            end = data.get('end_time')
            if not start or not end:
                raise serializers.ValidationError(
                    "start_time and end_time are required for working days."
                )
            if start >= end:
                raise serializers.ValidationError(
                    "start_time must be before end_time."
                )
            break_start = data.get('break_start')
            break_end = data.get('break_end')
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


class SchedulePutSerializer(serializers.Serializer):
    """PUT /schedule — full replacement (all 7 days required)."""
    schedule = WorkingHoursSerializer(many=True)

    def validate_schedule(self, value):
        days = [item['day_of_week'] for item in value]
        if len(days) != len(set(days)):
            raise serializers.ValidationError("Duplicate day_of_week values.")
        if set(days) != set(range(7)):
            raise serializers.ValidationError(
                "PUT requires all 7 days (0=Mon through 6=Sun)."
            )
        return value


class SchedulePatchSerializer(serializers.Serializer):
    """PATCH /schedule — partial update (1 or more days)."""
    schedule = WorkingHoursSerializer(many=True)

    def validate_schedule(self, value):
        if not value:
            raise serializers.ValidationError("schedule must not be empty.")
        days = [item['day_of_week'] for item in value]
        if len(days) != len(set(days)):
            raise serializers.ValidationError("Duplicate day_of_week values.")
        return value


class TimeOffCreateSerializer(serializers.Serializer):
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    reason = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, data):
        if data['end_at'] <= data['start_at']:
            raise serializers.ValidationError("end_at must be after start_at.")
        return data


# ---------------------------------------------------------------------------
# Response formatters
# ---------------------------------------------------------------------------

def _wh_to_dict(wh: SpecialistWorkingHours) -> dict:
    return {
        'day_of_week': wh.day_of_week,
        'day_name': wh.get_day_of_week_display(),
        'is_working_day': wh.is_working_day,
        'start_time': wh.start_time.strftime('%H:%M') if wh.start_time else None,
        'end_time': wh.end_time.strftime('%H:%M') if wh.end_time else None,
        'break_start': wh.break_start.strftime('%H:%M') if wh.break_start else None,
        'break_end': wh.break_end.strftime('%H:%M') if wh.break_end else None,
    }


def _to_to_dict(to: SpecialistTimeOff) -> dict:
    return {
        'id': str(to.id),
        'start_at': to.start_at.isoformat(),
        'end_at': to.end_at.isoformat(),
        'reason': to.reason,
        'created_at': to.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Working Hours Views (DRF-131)
# ---------------------------------------------------------------------------

class ScheduleView(APIView):
    """
    GET   /api/v1/specialists/me/schedule/  — Return 7-day schedule
    PUT   /api/v1/specialists/me/schedule/  — Replace full schedule (all 7 days)
    PATCH /api/v1/specialists/me/schedule/  — Update specific days (partial)

    Pro app only. Requires role=specialist.
    """
    permission_classes = [permissions.IsAuthenticated, IsSpecialist]

    def _get_specialist(self, request: Request):
        try:
            return request.user.specialist_profile
        except Exception:
            return None

    def get(self, request: Request) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        hours = (
            SpecialistWorkingHours.objects
            .filter(specialist=specialist)
            .order_by('day_of_week')
        )
        existing = {wh.day_of_week: wh for wh in hours}

        # Return all 7 days — fill defaults for missing ones
        day_names = dict(SpecialistWorkingHours.DayOfWeek.choices)
        result = []
        for day in range(7):
            if day in existing:
                result.append(_wh_to_dict(existing[day]))
            else:
                result.append({
                    'day_of_week': day,
                    'day_name': day_names[day],
                    'is_working_day': False,
                    'start_time': None,
                    'end_time': None,
                    'break_start': None,
                    'break_end': None,
                })

        return success_response(result)

    def put(self, request: Request) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        serializer = SchedulePutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        schedule_data = serializer.validated_data['schedule']

        # Atomic replace: delete all + recreate
        SpecialistWorkingHours.objects.filter(specialist=specialist).delete()
        SpecialistWorkingHours.objects.bulk_create([
            SpecialistWorkingHours(
                specialist=specialist,
                day_of_week=item['day_of_week'],
                is_working_day=item['is_working_day'],
                start_time=item.get('start_time'),
                end_time=item.get('end_time'),
                break_start=item.get('break_start'),
                break_end=item.get('break_end'),
            )
            for item in schedule_data
        ])

        max_ahead = getattr(settings, 'BOOKING_MAX_AHEAD_DAYS', 60)
        today = date.today()
        _invalidate_slots(specialist.id, today, today + timedelta(days=max_ahead))

        hours = (
            SpecialistWorkingHours.objects
            .filter(specialist=specialist)
            .order_by('day_of_week')
        )
        return success_response([_wh_to_dict(wh) for wh in hours])

    def patch(self, request: Request) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        serializer = SchedulePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        for item in serializer.validated_data['schedule']:
            SpecialistWorkingHours.objects.update_or_create(
                specialist=specialist,
                day_of_week=item['day_of_week'],
                defaults={
                    'is_working_day': item['is_working_day'],
                    'start_time': item.get('start_time'),
                    'end_time': item.get('end_time'),
                    'break_start': item.get('break_start'),
                    'break_end': item.get('break_end'),
                },
            )

        max_ahead = getattr(settings, 'BOOKING_MAX_AHEAD_DAYS', 60)
        today = date.today()
        _invalidate_slots(specialist.id, today, today + timedelta(days=max_ahead))

        hours = (
            SpecialistWorkingHours.objects
            .filter(specialist=specialist)
            .order_by('day_of_week')
        )
        return success_response([_wh_to_dict(wh) for wh in hours])


# ---------------------------------------------------------------------------
# TimeOff Views (DRF-132)
# ---------------------------------------------------------------------------

class TimeOffListView(APIView):
    """
    GET  /api/v1/specialists/me/time-off/  — List time-off blocks
    POST /api/v1/specialists/me/time-off/  — Create time-off block

    Query params for GET:
      date_from=YYYY-MM-DD  — blocks that end on or after this date
      date_to=YYYY-MM-DD    — blocks that start on or before this date

    POST checks for active appointments before allowing the block.
    """
    permission_classes = [permissions.IsAuthenticated, IsSpecialist]

    def _get_specialist(self, request: Request):
        try:
            return request.user.specialist_profile
        except Exception:
            return None

    def get(self, request: Request) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        qs = SpecialistTimeOff.objects.filter(specialist=specialist)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(end_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(start_at__date__lte=date_to)

        return success_response([_to_to_dict(to) for to in qs])

    def post(self, request: Request) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        serializer = TimeOffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        start_at = serializer.validated_data['start_at']
        end_at = serializer.validated_data['end_at']

        # Block creation if there are active appointments in the range
        from appointments.models import Appointment
        active_count = Appointment.objects.filter(
            specialist=specialist,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=end_at,
            end_datetime__gt=start_at,
        ).count()

        if active_count > 0:
            return error_response(
                "HAS_ACTIVE_APPOINTMENTS",
                f"Cannot block time: {active_count} active appointment(s) overlap this period.",
                status_code=409,
            )

        time_off = SpecialistTimeOff.objects.create(
            specialist=specialist,
            start_at=start_at,
            end_at=end_at,
            reason=serializer.validated_data.get('reason', ''),
        )

        _invalidate_slots(specialist.id, start_at.date(), end_at.date())

        return success_response(_to_to_dict(time_off), status_code=201)


class TimeOffDetailView(APIView):
    """
    DELETE /api/v1/specialists/me/time-off/{id}/  — Remove time-off block
    """
    permission_classes = [permissions.IsAuthenticated, IsSpecialist]

    def _get_specialist(self, request: Request):
        try:
            return request.user.specialist_profile
        except Exception:
            return None

    def delete(self, request: Request, pk) -> Response:
        specialist = self._get_specialist(request)
        if not specialist:
            return error_response(
                "NOT_FOUND", "Specialist profile not found.", status_code=404,
            )

        try:
            time_off = SpecialistTimeOff.objects.get(id=pk, specialist=specialist)
        except SpecialistTimeOff.DoesNotExist:
            return error_response("NOT_FOUND", "Time-off not found.", status_code=404)

        _invalidate_slots(specialist.id, time_off.start_at.date(), time_off.end_at.date())
        time_off.delete()

        return Response(status=204)
