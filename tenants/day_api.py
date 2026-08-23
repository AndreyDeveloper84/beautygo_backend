"""The salon-admin day journal — GET /api/v1/tenants/me/day/ (DRF-1063).

Lives on the tenant route, not under ``/api/v1/internal/``, and that is
load-bearing rather than stylistic: ``TenantContextMiddleware`` excludes
the internal tree, so ``request.tenant`` is always ``None`` there and
``IsTenantAdmin`` — which fails closed without an addressed tenant —
could never pass. An internal-tree day endpoint would have to take the
tenant from the body, which is exactly the thing the salon-admin surface
does not do.

Read-only by construction. Every change the journal displays is made
through a command owned by the appointment domain; this view has no
write path to reach for.
"""
from __future__ import annotations

import logging

from django.utils.dateparse import parse_date
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from appointments.application.services.tenant_day_service import (
    build_tenant_day,
)
from users.authentication import AylaServiceBearerAuthentication
from users.permissions import (
    IsProApp,
    IsTenantAdmin,
    ServiceCredentialIsReadOnly,
)
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class TenantDayView(APIView):
    """GET /api/v1/tenants/me/day/?date=YYYY-MM-DD

    Masters of this salon, their hours, breaks, absences and bookings for
    one date. Defaults to today in the salon's own reckoning.
    """

    # DRF-1297 B-1 -- this is Ayla's primary read of the salon, and it
    # is deliberately the SAME view the console uses, not a parallel
    # projection. The Ayla authenticator runs before JWT and steps aside
    # for anything that is not its token, so the console path is byte-for
    # -byte unchanged; the permission list below is unchanged too.
    #
    # What that buys: Ayla's tenant still comes from X-Tenant through
    # middleware, and IsTenantAdmin still proves that the human named in
    # X-External-User-ID administers THAT salon. Presenting the service
    # token and naming someone else's slug reads nothing.
    authentication_classes = [
        AylaServiceBearerAuthentication,
        *api_settings.DEFAULT_AUTHENTICATION_CLASSES,
    ]

    # Same shape as the only other IsTenantAdmin consumer (the revoke
    # endpoint): IsProApp gates the surface to provider-side callers,
    # defeating a stolen admin JWT replayed from a client build, and
    # IsTenantAdmin enforces the (admin, tenant) tuple with the tenant
    # taken from middleware.
    permission_classes = [
        permissions.IsAuthenticated,
        IsProApp,
        IsTenantAdmin,
        # The view is GET-only, so this is belt-and-braces rather than
        # load-bearing here -- but it is what stops a later `post` on
        # this class from inheriting Ayla's read grant by accident.
        ServiceCredentialIsReadOnly,
    ]

    @extend_schema(
        tags=["tenants"],
        parameters=[
            OpenApiParameter(
                name="date", required=False, type=str,
                description=(
                    "Calendar date in the master's local timezone "
                    "(YYYY-MM-DD). Defaults to today."
                ),
            ),
        ],
        responses={200: None, 400: None, 403: None},
    )
    def get(self, request: Request) -> Response:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            # Unreachable through IsTenantAdmin, which already fails
            # closed on a missing tenant. Kept so a future permission
            # change cannot turn this into an AttributeError.
            return error_response(
                "TENANT_REQUIRED",
                "Заголовок X-Tenant обязателен.",
                status_code=403,
            )

        raw = (request.query_params.get("date") or "").strip()
        if raw:
            target_date = parse_date(raw)
            if target_date is None:
                return error_response(
                    "VALIDATION_ERROR",
                    "Параметр date должен быть датой в формате YYYY-MM-DD.",
                    status_code=400,
                )
        else:
            target_date = self._today_for(tenant)

        day = build_tenant_day(tenant, target_date)
        return success_response(day.to_dict())

    @staticmethod
    def _today_for(tenant):
        """"Today" as the salon means it, not as the server means it.

        Taken from the masters' own timezone rather than the Django
        default: a request just after midnight UTC from a salon in
        Moscow must not open yesterday's journal.
        """
        from datetime import date as date_cls
        from zoneinfo import ZoneInfo

        from django.utils import timezone as dj_timezone

        from users.models import SpecialistProfile

        tz_name = (
            SpecialistProfile.objects
            .filter(tenant=tenant)
            .values_list("timezone", flat=True)
            .first()
        )
        if not tz_name:
            return dj_timezone.localdate()
        try:
            return dj_timezone.now().astimezone(ZoneInfo(tz_name)).date()
        except Exception:  # noqa: BLE001 — a bad tz string must not 500
            logger.warning(
                "tenant.day.bad_timezone tenant=%s tz=%r", tenant.slug, tz_name,
            )
            return date_cls.today()
