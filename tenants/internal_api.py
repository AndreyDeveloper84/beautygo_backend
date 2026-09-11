"""``POST /api/v1/internal/tenants/`` — салон по slug для бота (DRF-1525).

Вызывающий — экран «подключить салон» админки бота. Он присылает
``slug`` / ``name`` / ``city`` и получает UUID салона, который дальше
служит общим ключом синхронизации (``?tenant=<uuid>`` в зеркале). Сам
человек UUID не вводит и не видит — решение владельца 11.09.2026.

### Почему свой провижининг-токен, а не общий Bearer и не identity-токен

Ручка ЗАВОДИТ строку в источнике истины. Общий ``AYLA_INTERNAL_API_TOKEN``
лежит в рантайме бота и даёт право читать зеркало и писать записи;
право заводить салоны — другая сила. Но и не та, что у ``bind-external``:
заведение салона не присваивает никому личность, а identity-токен боту
выдавать запрещено (OPEN_DECISIONS §151). Поэтому — свой секрет,
``AYLA_TENANT_PROVISIONING_TOKEN``, сторож :class:`IsTenantProvisioningBearer`
(DRF-1695, C1). Фейлится ЗАКРЫТО: пустой токен — 403, не 500 и не 201.
Бот обязан читать этот 403 как «токен не задан → ``SETUP_PENDING``», а не
как сбой.

### Ответы

* ``201`` — салон заведён; ``200`` — уже был, тот же по имени (повтор
  безвреден);
* ``409 TENANT_SLUG_TAKEN`` — slug занят салоном с другим названием;
  тело называет существующее имя, чтобы оператор решил, опечатка это
  или второй салон;
* ``400`` — форма запроса; ``403`` — сторож (см. выше).

``city`` наружу уезжает как ``null``, когда пуст, — тот же контракт, что
у ``_InternalTenantFieldMixin`` (OPEN_DECISIONS §65: отсутствие доезжает
отсутствием).
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from tenants.provisioning import TenantNameMismatch, ensure_tenant
from users.permissions import IsTenantProvisioningBearer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class _EnsureTenantRequestSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=50)
    name = serializers.CharField(max_length=200)
    city = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default="",
    )


class _TenantResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()
    city = serializers.CharField(allow_null=True)
    is_active = serializers.BooleanField()


def _payload(tenant) -> dict:
    return {
        "id": str(tenant.id),
        "slug": tenant.slug,
        "name": tenant.name,
        "city": tenant.city or None,
        "is_active": tenant.is_active,
    }


class InternalEnsureTenantView(APIView):
    """См. докстринг модуля."""

    authentication_classes: list = []
    permission_classes = [IsTenantProvisioningBearer]
    serializer_class = _EnsureTenantRequestSerializer

    @extend_schema(
        operation_id="internal_tenants_ensure",
        tags=["internal"],
        request=_EnsureTenantRequestSerializer,
        responses={
            200: _TenantResponseSerializer,
            201: _TenantResponseSerializer,
            400: OpenApiResponse(description="Malformed slug / name / city"),
            403: OpenApiResponse(
                description="Missing / invalid provisioning bearer token "
                            "(the general bot service token is NOT accepted; "
                            "an empty token disables the endpoint)",
            ),
            409: OpenApiResponse(
                description="Slug already belongs to a tenant with a different name",
            ),
        },
        description=(
            "PROVISIONING-ONLY: return the tenant with this slug, creating it "
            "when absent. Idempotent by slug: a repeat with the same name is "
            "200 with the same row; the same slug with a different name is "
            "409 naming the existing tenant. Nothing on an existing row is "
            "ever updated here."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = _EnsureTenantRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            tenant, created = ensure_tenant(
                slug=data["slug"], name=data["name"], city=data["city"],
            )
        except TenantNameMismatch as exc:
            logger.info(
                "tenants.ensure.slug_taken slug=%s existing=%r requested=%r",
                exc.slug, exc.existing_name, exc.requested_name,
            )
            return error_response(
                ErrorCode.TENANT_SLUG_TAKEN,
                f"Slug {exc.slug!r} уже занят салоном {exc.existing_name!r}.",
                details={
                    "slug": exc.slug,
                    "existing_name": exc.existing_name,
                    "requested_name": exc.requested_name,
                },
                status_code=status.HTTP_409_CONFLICT,
            )

        logger.info(
            "tenants.ensure.%s slug=%s tenant=%s",
            "created" if created else "existing", tenant.slug, tenant.id,
        )
        return success_response(
            _payload(tenant),
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
