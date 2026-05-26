"""POST /api/v1/tenants/me/relationships/{user_id}/revoke/ — #246 Q1.

Admin-side endpoint for revoking a customer's (or staff's) tenant
relationship. Actor is the salon admin; ``request.tenant`` (resolved
by ``TenantContextMiddleware`` from the X-Tenant header / JWT claim)
identifies the salon scope.

Auth: ``IsTenantAdmin`` — caller must have an active admin-role TUR in
``request.tenant``. The permission's tenant check is the second factor
defeating cross-tenant impersonation: an admin of salon A cannot
revoke users in salon B even if they swap headers, because the TUR
lookup binds to ``request.tenant`` AND the admin's own membership.

Behaviour summary (founder Q1 ack 2026-05-26):

- Body: ``{reason: str, notify_customer: bool=false}``.
- DEFAULT silent — push NOT sent unless ``notify_customer=true``.
- When notify_customer is true and role=customer, generic push
  ("Связь с салоном X прекращена") is queued. No internal reason
  exposed.
- ALWAYS emits ``tenant.relationship.revoked`` OutboxEvent (audit
  trail). Consumers dedupe on event_id.

Q2 cascade (specialist departure → booking refunds) is handled in a
separate service; this endpoint only handles the relationship-row
side of revoke.
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsTenantAdmin
from users.response import error_response, success_response
from users.services import (
    TURNotFoundError,
    revoke_tenant_user_relationship,
)


logger = logging.getLogger(__name__)


class TenantRelationshipRevokeRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=256,
        help_text=(
            "Internal reason (audit-only, not exposed to user). "
            "Examples: 'kicked_for_no_show', 'master_left', "
            "'salon_closing'."
        ),
    )
    notify_customer = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "If true, queue a generic push notification to the user "
            "(role=customer only). Default false — most revokes are "
            "silent per founder Q1 ack."
        ),
    )


class TenantRelationshipRevokeView(APIView):
    """POST /api/v1/tenants/me/relationships/{user_id}/revoke/

    Salon admin revokes a TenantUserRelationship in their own tenant.
    """
    permission_classes = [permissions.IsAuthenticated, IsTenantAdmin]
    serializer_class = TenantRelationshipRevokeRequestSerializer

    @extend_schema(
        tags=["tenants"],
        request=TenantRelationshipRevokeRequestSerializer,
        responses={
            204: OpenApiResponse(
                description="Relationship revoked; outbox event emitted.",
            ),
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Not a tenant admin"),
            404: OpenApiResponse(
                description=(
                    "No active relationship for (user, tenant). Info-"
                    "hidden — does NOT disclose whether the user "
                    "exists, was never in this tenant, or was already "
                    "revoked."
                ),
            ),
        },
    )
    def post(self, request: Request, user_id) -> Response:
        serializer = TenantRelationshipRevokeRequestSerializer(
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]
        notify_customer = serializer.validated_data["notify_customer"]

        # Resolve target user. We do not pre-disclose existence; if
        # they're not in our tenant, the revoke service raises
        # TURNotFoundError → uniform 404.
        from users.models import User
        try:
            target = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            logger.info(
                "tur.revoke user_not_found actor=%s tenant=%s target=%s",
                request.user.pk, request.tenant.pk, user_id,
            )
            return error_response(
                "NOT_FOUND",
                "Связь не найдена.",
                status_code=404,
            )

        try:
            revoke_tenant_user_relationship(
                target_user=target,
                tenant=request.tenant,
                actor=request.user,
                reason=reason,
                notify_customer=notify_customer,
            )
        except TURNotFoundError:
            logger.info(
                "tur.revoke tur_not_found actor=%s tenant=%s target=%s",
                request.user.pk, request.tenant.pk, target.pk,
            )
            return error_response(
                "NOT_FOUND",
                "Связь не найдена.",
                status_code=404,
            )

        # 204 — no content. The audit OutboxEvent + (optional)
        # notification carry the operational data; the HTTP response
        # is just the ack.
        return success_response(None, status_code=204)
