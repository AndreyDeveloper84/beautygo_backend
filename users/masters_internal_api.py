"""POST /api/v1/masters/internal/by-yclients-staff-ids/ — task #92.

W1 admin mini-app batch lookup: given a list of YClients staff ids +
a tenant, return the matching Ayla ``SpecialistProfile`` rows plus the
ids that didn't resolve.

Auth: ``IsInternalBearer`` (Authorization: Bearer matching
``AYLA_INTERNAL_API_TOKEN``). Catalog-shaped endpoint — no
``X-External-User-ID`` or per-user filter; the W1 admin operator is
not impersonating a specific Ayla user. Defense-in-depth: the body
requires ``tenant_id`` explicitly, so a leaked bearer cannot enumerate
across tenants without naming each one.
"""
from __future__ import annotations

import logging
from typing import Any

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import SpecialistProfile
from users.permissions import IsInternalBearer
from users.response import success_response


logger = logging.getLogger(__name__)


# Cap on how many ids one batch may resolve. Keeps the IN-clause shape
# bounded and pushes very-large workloads to paginate. Picked at 200
# because the W1 admin operator typically syncs one salon at a time;
# 200 is well above the largest salon roster (~80 masters) we expect
# pre-pilot.
MAX_BATCH_SIZE = 200


class MastersByYclientsStaffIdsRequestSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField(
        help_text="Tenant the batch lookup is scoped to.",
    )
    yclients_staff_ids = serializers.ListField(
        child=serializers.CharField(max_length=64),
        min_length=1,
        max_length=MAX_BATCH_SIZE,
        help_text=(
            "YClients per-master staff ids. 1–200 items per request."
        ),
    )


class _MasterItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    yclients_staff_id = serializers.CharField()
    display_name = serializers.CharField()
    tenant_id = serializers.UUIDField()
    status = serializers.CharField()


class MastersByYclientsStaffIdsView(APIView):
    """POST /api/v1/masters/internal/by-yclients-staff-ids/

    Resolves a batch of YClients staff ids to ``SpecialistProfile`` rows
    within one tenant. Tenant boundary is required (body field) so a
    leaked bearer cannot enumerate across tenants without iterating
    each tenant id explicitly.

    Response shape::

        {
          "data": {
            "masters": [ {id, yclients_staff_id, display_name,
                          tenant_id, status}, ... ],
            "not_found_staff_ids": ["123", "456", ...]
          }
        }

    Order of ``masters`` follows the SQL output (no guarantee), but
    ``not_found_staff_ids`` preserves the request order minus any that
    matched — useful for the admin UI to highlight gaps.
    """
    # Disable DRF's default JWTAuthentication: the bot ships an
    # Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>, which is NOT a
    # JWT — leaving JWTAuthentication in place would 401-reject the
    # request before IsInternalBearer ever runs. See PR #158 fix.
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = MastersByYclientsStaffIdsRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=MastersByYclientsStaffIdsRequestSerializer,
        responses={
            200: inline_serializer(
                name="MastersByYclientsStaffIdsResponse",
                fields={
                    "data": inline_serializer(
                        name="MastersByYclientsStaffIdsResponseData",
                        fields={
                            "masters": _MasterItemSerializer(many=True),
                            "not_found_staff_ids": serializers.ListField(
                                child=serializers.CharField(),
                            ),
                        },
                    ),
                },
            ),
            400: OpenApiResponse(description="Validation error"),
            401: OpenApiResponse(description="Bearer token missing or invalid"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = MastersByYclientsStaffIdsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tenant_id = serializer.validated_data["tenant_id"]
        # De-duplicate while preserving first-seen order so the response
        # not_found_staff_ids matches the caller's mental model.
        seen: set[str] = set()
        requested_ids: list[str] = []
        for raw in serializer.validated_data["yclients_staff_ids"]:
            if raw and raw not in seen:
                seen.add(raw)
                requested_ids.append(raw)

        # Filter empties — DRF's CharField allows non-empty by default
        # (allow_blank=False), but we guard explicitly because the
        # SpecialistProfile.yclients_staff_id column default is "" and
        # an unscoped IN with "" would pull every legacy row.
        requested_ids = [s for s in requested_ids if s]
        if not requested_ids:
            return success_response({
                "masters": [],
                "not_found_staff_ids": [],
            })

        rows = (
            SpecialistProfile.objects
            .filter(
                tenant_id=tenant_id,
                yclients_staff_id__in=requested_ids,
            )
            .only(
                "id", "yclients_staff_id", "display_name",
                "tenant_id", "status",
            )
        )

        masters: list[dict[str, Any]] = []
        found_staff_ids: set[str] = set()
        for row in rows:
            masters.append({
                "id": str(row.id),
                "yclients_staff_id": row.yclients_staff_id,
                "display_name": row.display_name,
                "tenant_id": str(row.tenant_id) if row.tenant_id else None,
                "status": row.status,
            })
            found_staff_ids.add(row.yclients_staff_id)

        not_found = [
            sid for sid in requested_ids if sid not in found_staff_ids
        ]

        logger.info(
            "masters.batch_lookup tenant=%s requested=%d found=%d",
            tenant_id, len(requested_ids), len(masters),
        )

        return success_response({
            "masters": masters,
            "not_found_staff_ids": not_found,
        })
