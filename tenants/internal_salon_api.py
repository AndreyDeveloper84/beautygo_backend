"""``GET /api/v1/internal/salons/<slug>/readiness/`` — салонная готовность поимённо (DRF-2117).

Читает салонный бот от имени владельца / администратора, которого он
обслуживает: ``Authorization: Bearer AYLA_INTERNAL_API_TOKEN`` +
``X-External-User-ID`` — та же пара, что на ``/tenants/me/…`` (OD-B5-1).
Сторож — :class:`users.permissions.IsInternalBearerForSalonSubject`: актор
резолвится без создания строк и обязан держать активную TUR ``admin`` в
активном тенанте ``<slug>``; чужой, неизвестный или выключенный салон — 404
(slug чужого тенанта не подтверждается).

Ответ — :func:`tenants.salon_readiness.salon_readiness` как есть; форма —
в ``docs/CATALOG_INTERNAL_API_CONTRACT.md``. Это половина ответа, которую
знает каталог; §83 / ``catalog_specialist_id`` / ``sellable`` зеркала знает
только бот и накладывает сам (докстринг модуля агрегата).
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from tenants.salon_readiness import salon_readiness
from users.permissions import IsInternalBearerForSalonSubject
from users.response import success_response

logger = logging.getLogger(__name__)


class InternalSalonReadinessView(APIView):
    """См. докстринг модуля."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSalonSubject]
    subject_url_kwarg = "slug"

    @extend_schema(
        operation_id="internal_salon_readiness",
        tags=["internal"],
        responses={
            200: OpenApiResponse(
                description=(
                    "{salon, ready, checked_at, horizon_days, masters[], problems[], limits[]} "
                    "— see docs/CATALOG_INTERNAL_API_CONTRACT.md"
                ),
            ),
            403: OpenApiResponse(
                description="Not the runtime bearer, X-External-User-ID missing, "
                            "actor unknown or inactive",
            ),
            404: OpenApiResponse(
                description="No active salon with this slug administered by the actor",
            ),
        },
        description=(
            "Per-master readiness of a salon as the catalog sees it: publication, "
            "schedule, sellable services, MAX identity link, free slots within "
            "7 days. `ready` only with an empty problem list and no unknown check."
        ),
    )
    def get(self, request: Request, slug: str) -> Response:
        # Тенант положил сторож — тот же объект, по которому он подтвердил TUR;
        # второе чтение по slug могло бы разойтись с первым.
        tenant = request.salon_tenant
        result = salon_readiness(tenant)
        logger.info(
            "salon_readiness.read tenant=%s masters=%s problems=%s ready=%s",
            tenant.slug, len(result.masters), len(result.problems), result.ready,
        )
        return success_response(result.as_dict())
