"""``POST /api/v1/internal/tenants/<slug>/salon-admins/`` — DRF-2085.

Ручка одной силы: свежая учётка администратора салона + TUR ``admin`` +
связь с MAX-личностью, под собственным credential
(:class:`users.permissions.IsSalonAdminLinkBearer`). Логика — в
:mod:`users.salon_admin_linking`; здесь форма запроса, статус по причине
и лимит.

Вызывающий — операторское действие в Django Admin бота
(``platform_operations``), не рантайм и не Mini App: credential в
браузер не уезжает по построению — его держит контейнер бота, а не
клиентский код.

### Ответы

* ``201`` — учётка, TUR и связь созданы и подтверждены readback'ом;
  ``200`` — повтор с тем же ``idempotency_key`` (те же id, ничего нового);
* ``400 VALIDATION_ERROR`` — форма запроса; ``400 SALON_ADMIN_LINK_REFUSED``
  ``invalid_external_user_id``;
* ``403`` — сторож: нет/чужой/пустой credential (общий Bearer и оба
  provisioning-токена НЕ принимаются);
* ``404 SALON_ADMIN_LINK_REFUSED tenant_not_found``;
* ``409 SALON_ADMIN_LINK_REFUSED`` — ``tenant_inactive`` ·
  ``identity_already_bound`` · ``identity_not_proxy`` ·
  ``idempotency_key_reused`` · ``bind_refused`` — FAIL_CLOSED, ничего не
  создано, разрешает оператор каталога руками;
* ``429`` — лимит ``salon_admin_link``;
* ``500 SALON_ADMIN_LINK_REFUSED readback_failed`` — строки есть, но
  салонная поверхность их не подтвердила; повтор с тем же ключом
  повторит readback.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsSalonAdminLinkBearer
from users.response import error_response, success_response
from users.salon_admin_linking import SalonAdminLinkRefused, link_salon_admin

logger = logging.getLogger(__name__)


class _SalonAdminLinkRequestSerializer(serializers.Serializer):
    external_user_id = serializers.CharField(max_length=200)
    actor = serializers.CharField(max_length=200)
    correlation_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="",
    )
    idempotency_key = serializers.CharField(max_length=64, min_length=8)


class _SalonAdminLinkResponseSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField()
    slug = serializers.CharField()
    ayla_user_id = serializers.UUIDField()
    relationship_id = serializers.UUIDField()
    role = serializers.CharField()
    status = serializers.CharField()


class InternalSalonAdminLinkView(APIView):
    """OPERATOR-ONLY capability (OWNER RULING 18.09, DRF-2085): see module."""

    authentication_classes: list = []
    permission_classes = [IsSalonAdminLinkBearer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "salon_admin_link"
    serializer_class = _SalonAdminLinkRequestSerializer

    @extend_schema(
        operation_id="internal_salon_admin_link",
        tags=["internal"],
        request=_SalonAdminLinkRequestSerializer,
        responses={
            200: _SalonAdminLinkResponseSerializer,
            201: _SalonAdminLinkResponseSerializer,
            400: OpenApiResponse(description="Malformed body / invalid external_user_id"),
            403: OpenApiResponse(
                description="Missing / invalid salon-admin-link bearer (general, identity "
                            "and tenant provisioning tokens are NOT accepted)",
            ),
            404: OpenApiResponse(description="No tenant with this slug (details.reason)"),
            409: OpenApiResponse(
                description="FAIL_CLOSED: tenant inactive, identity already bound or not a "
                            "proxy, idempotency key reused with another body (details.reason)",
            ),
            429: OpenApiResponse(description="salon_admin_link rate limit"),
        },
        description=(
            "OPERATOR-ONLY (DRF-2085, owner ruling 18.09 variant A): create a FRESH "
            "salon-administrator account, its admin TenantUserRelationship in the "
            "named salon, and bind the given MAX identity to it — one transaction, "
            "SUCCESS only after authoritative readback. Never touches an existing "
            "account or relationship; a bound identity, a real account under the "
            "external id, or a reused idempotency key is refused by name."
        ),
    )
    def post(self, request: Request, slug: str) -> Response:
        serializer = _SalonAdminLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            link = link_salon_admin(
                tenant_slug=slug,
                external_user_id=data["external_user_id"],
                actor=data["actor"],
                correlation_id=data["correlation_id"],
                idempotency_key=data["idempotency_key"],
            )
        except SalonAdminLinkRefused as exc:
            return error_response(
                ErrorCode.SALON_ADMIN_LINK_REFUSED,
                "Salon administrator not linked.",
                details={"reason": exc.reason},
                status_code=exc.status_code,
            )

        return success_response(
            {
                "tenant_id": str(link.tenant.id),
                "slug": link.tenant.slug,
                "ayla_user_id": str(link.user.id),
                "relationship_id": str(link.relationship.id),
                "role": link.relationship.role,
                "status": "created" if link.created else "replayed",
            },
            status_code=status.HTTP_201_CREATED if link.created else status.HTTP_200_OK,
        )


__all__ = ["InternalSalonAdminLinkView"]
