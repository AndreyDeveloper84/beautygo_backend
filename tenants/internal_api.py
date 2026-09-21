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
from tenants.models import Tenant
from tenants.provisioning import TenantNameMismatch, ensure_tenant
from tenants.solo_provisioning import SoloProvisioningRefused, provision_solo_workspace
from users.permissions import IsInternalBearer, IsTenantProvisioningBearer
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

# ─── DRF-1828 (G1/G4): solo-workspace — Tenant с UUID бота + DRAFT-профиль ───


class _SoloWorkspaceRequestSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField()
    slug = serializers.SlugField(max_length=50)
    name = serializers.CharField(max_length=200)
    city = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default="",
    )
    external_user_id = serializers.CharField(max_length=200)
    display_name = serializers.CharField(max_length=255)


class _SoloWorkspaceResponseSerializer(serializers.Serializer):
    tenant_id = serializers.UUIDField()
    slug = serializers.CharField()
    specialist_id = serializers.UUIDField()
    user_id = serializers.UUIDField()
    status = serializers.CharField()


class InternalSoloWorkspaceView(APIView):
    """PROVISIONING-ONLY: завести solo-workspace или вернуть уже заведённый.

    См. ``tenants/solo_provisioning.py``. Тот же сторож, что у ``/internal/tenants/``;
    общий и identity-токены отвергаются. Входов, адресующих существующего
    специалиста или тенант, нет — на существующих строках ничего не
    обновляется.
    """

    authentication_classes: list = []
    permission_classes = [IsTenantProvisioningBearer]
    serializer_class = _SoloWorkspaceRequestSerializer

    @extend_schema(
        operation_id="internal_solo_workspace_provision",
        tags=["internal"],
        request=_SoloWorkspaceRequestSerializer,
        responses={
            200: _SoloWorkspaceResponseSerializer,
            201: _SoloWorkspaceResponseSerializer,
            400: OpenApiResponse(description="Malformed body"),
            403: OpenApiResponse(
                description="Missing / invalid provisioning bearer token "
                            "(general and identity tokens are NOT accepted)",
            ),
            409: OpenApiResponse(
                description="slug / tenant_id taken by another tenant, or the "
                            "claim is bound to another workspace (details.reason)",
            ),
        },
        description=(
            "PROVISIONING-ONLY (DRF-1828, owner ruling G1/G4): create the solo "
            "master's catalog workspace — Tenant with the bot's UUID and "
            "kind=solo, a working specialist User, SpecialistProfile(DRAFT) "
            "claimed by external_user_id, admin TenantUserRelationship — in one "
            "transaction. Idempotent by external_user_id: a repeat with the same "
            "tenant_id/slug is 200 with the same ids. Nothing on an existing row "
            "is ever updated here; pre-LINKED identity is NOT asserted."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = _SoloWorkspaceRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            workspace = provision_solo_workspace(
                tenant_id=data["tenant_id"],
                slug=data["slug"],
                name=data["name"],
                city=data["city"],
                external_user_id=data["external_user_id"],
                display_name=data["display_name"],
            )
        except SoloProvisioningRefused as exc:
            # DRF-1874: чужие идентификаторы (id/slug занявшего тенанта) —
            # оператору в лог; наружу, даже держателю provisioning-токена,
            # только причина.
            logger.info(
                "tenants.solo_provisioning.refused reason=%s slug=%s tenant_id=%s details=%s",
                exc.reason, data["slug"], data["tenant_id"], exc.details,
            )
            return error_response(
                ErrorCode.SOLO_PROVISIONING_REFUSED,
                "Solo workspace not provisioned.",
                details={"reason": exc.reason},
                status_code=status.HTTP_409_CONFLICT,
            )

        return success_response(
            {
                "tenant_id": str(workspace.tenant.id),
                "slug": workspace.tenant.slug,
                "specialist_id": str(workspace.profile.id),
                "user_id": str(workspace.user.id),
                "status": workspace.profile.status,
            },
            status_code=status.HTTP_201_CREATED if workspace.created else status.HTTP_200_OK,
        )


# ─── DRF-2254: вид тенанта — единственный источник «чьё место и кто ведёт услуги» ───


class _TenantKindResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=Tenant.Kind.choices)


class InternalTenantKindView(APIView):
    """``GET /internal/tenants/<uuid>/kind/`` — вид тенанта для бота (DRF-2254).

    Бот решал «соло» подсчётом людей в тенанте (``is_solo_provider``), каталог
    — этим признаком; экраны самообслуживания мастера (место, услуги) бот
    открывал по первому, а каталог разрешал или отказывал по второму. Источник
    один — ``Tenant.kind``: бот читает его здесь и отдаёт в ``/me`` как
    ``workspace_kind``.

    Только чтение и только под общим внутренним токеном бота: provisioning-токен
    здесь не принимается (чтение не должно давать права заводить тенанты), а
    общий токен не принимают ручки provisioning выше.

    Чтение «по каталогу», без проверки субъекта: явный ``tenant_id`` — UUID в
    адресе; ответ — только ``{id, kind}``, без персональных данных.
    Деактивированный тенант — 404, как у ``IsInternalBearerForSalonSubject``:
    для мёртвого пространства бот не открывает экраны по его виду.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="internal_tenants_kind",
        tags=["internal"],
        responses={
            200: _TenantKindResponseSerializer,
            403: OpenApiResponse(
                description="Missing / invalid bot internal bearer token "
                            "(the provisioning token is NOT accepted; "
                            "an empty token disables the endpoint)",
            ),
            404: OpenApiResponse(description="No active tenant with this UUID"),
        },
        description="Вид тенанта (salon | solo) — единственный источник для гейта экранов самообслуживания мастера.",
    )
    def get(self, request: Request, tenant_id) -> Response:
        tenant = Tenant.objects.filter(pk=tenant_id).only("id", "kind").first()
        if tenant is None:
            return error_response(
                ErrorCode.TENANT_NOT_FOUND, "Tenant not found.", status_code=status.HTTP_404_NOT_FOUND,
            )
        return success_response({"id": str(tenant.id), "kind": tenant.kind})
