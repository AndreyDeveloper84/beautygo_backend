"""C2 — internal billing status endpoint (PILOT_CONTRACTS §3).

``GET /api/v1/internal/billing/specialists/{specialist_id}/status/`` —
Bearer ``AYLA_INTERNAL_API_TOKEN``. ``specialist_id`` is the Ayla **User
UUID** (AMD-005); resolution user → SpecialistProfile happens here.

URL mounting is W1-owned (djangoProject/urls.py patch, B-5); tests run
against the W2 shim urlconf (billing/tests/urls_w2.py).
"""
from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.services import build_billing_status
from users.models import SpecialistProfile
from users.permissions import IsInternalBearer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class BillingSpecialistStatusView(APIView):
    """C2: subscription status + pending fees + last invoice for a specialist."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_billing_specialist_status",
        tags=["internal", "billing"],
        responses={
            200: inline_serializer(
                name="InternalBillingStatus",
                fields={"data": serializers.DictField()},
            ),
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="Specialist does not exist"),
        },
        description=(
            "Billing status for the bot/miniapp (C2). 200 always for an "
            "existing specialist — no account yields status=none with "
            "null fields and zeroed fees. 404 only when the specialist "
            "does not exist (SPECIALIST_NOT_FOUND)."
        ),
    )
    def get(self, request: Request, specialist_id: UUID) -> Response:
        specialist = (
            SpecialistProfile.objects
            .filter(user_id=specialist_id)
            .select_related("user")
            .first()
        )
        if specialist is None:
            # C2: 404 ONLY for a non-existent specialist; an existing
            # specialist without billing data is 200 with nulls.
            logger.info(
                "internal.billing.specialist_not_found user_id=%s request_id=%s",
                specialist_id, getattr(request, "request_id", "-"),
            )
            return error_response(
                "SPECIALIST_NOT_FOUND", "Specialist not found.", status_code=404,
            )
        return success_response(build_billing_status(specialist))
