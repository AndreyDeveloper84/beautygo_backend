"""Internal Bearer surface for blocking a specialist's time (DRF-1062).

Why this exists. The bot owns the *interface* through which a master asks
for a day off — it lives in the Mini App and it works. What it does not
own is the schedule: once the customer picker reads slots from Ayla, an
approval written into the bot's own ``apps.scheduling`` changes nothing a
client can see. The administrator would approve a day off, be told it
succeeded, and the day would stay on sale. That silent disagreement
between two stores is the defect DRF-1062 exists to remove, so the
approval has to land here instead.

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

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES
from appointments.models import SpecialistTimeOff

from .permissions import IsInternalBearer
from .response import error_response, success_response
from .schedule_api import _invalidate_slots

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
