"""GET /api/v1/users/me/tenant-relationships/ — #246 sub-phase 1.F.

Canonical full-list endpoint for a user's active TenantUserRelationship
rows. Per founder ack 2026-05-25 Q4: bot-platform and mobile clients
call THIS endpoint to populate "your providers" UI, not the JWT claim.

JWT carries only the staff primary (or None for customers); this
endpoint returns ALL active relationships across all roles.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TenantUserRelationship
from .response import success_response


class _TenantRelationshipItemSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField()
    tenant_slug = serializers.CharField()
    tenant_name = serializers.CharField()
    role = serializers.ChoiceField(
        choices=TenantUserRelationship.Role.choices,
    )
    granted_at = serializers.DateTimeField()


class MyTenantRelationshipsView(APIView):
    """List the authenticated user's active tenant relationships.

    Per #246 design: customer is multi-provider by default — N active
    TUR rows allowed. JWT carries only the staff primary; this
    endpoint is the canonical source for the full list.

    Returns rows ordered by `-granted_at`, then `-id` (most-recent
    first). Mobile clients render this as "your providers" list;
    bot-platform reads it to populate W2 payment_failed and similar
    memory.

    DRF-1127 — why `-id` is there. `granted_at` is `auto_now_add`, so
    every row written inside one transaction (a bulk grant, an import,
    a single request that joins the user to several tenants) carries
    the SAME stamp. `-granted_at` alone is a partial order: the DB may
    return tied rows in any order it likes, and two identical requests
    may disagree. That is invisible until a client diffs the list or
    someone puts pagination on this endpoint, at which point tied rows
    start vanishing between pages. The secondary key makes the order
    total, so the answer is reproducible.

    Inactive (revoked) rows are NOT returned — this is the user's
    "current memberships" view, not an audit log.
    """
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        responses={
            200: inline_serializer(
                name="MyTenantRelationshipsResponse",
                fields={
                    "data": _TenantRelationshipItemSerializer(many=True),
                },
            ),
            401: OpenApiResponse(description="Unauthenticated"),
        },
    )
    def get(self, request: Request) -> Response:
        rows = (
            TenantUserRelationship.objects
            .filter(user=request.user, is_active=True)
            .select_related("tenant")
            .order_by("-granted_at", "-id")
        )
        return success_response({
            "data": [
                {
                    "tenant_id": str(row.tenant_id),
                    "tenant_slug": row.tenant.slug,
                    "tenant_name": row.tenant.name,
                    "role": row.role,
                    "granted_at": row.granted_at.isoformat(),
                }
                for row in rows
            ],
        })
