"""Подсказки адреса места мастера (M12a, DRF-1804; макет 5, кадр 5.2; P46).

``POST /api/v1/internal/specialists/{specialist_id}/geocoding/suggest/`` с телом ``{"q": "..."}``.

**Почему POST, а не GET ?q=.** Строка — адрес, который вводит мастер, часто
домашний. В URL она оседает в журналах доступа прокси и сервера приложения и в
адресе запроса в отчётах об ошибках; тело туда не пишется. По той же причине
``q`` нигде не логируется, не попадает в исключения и в ответ об ошибке: при
сбое провайдера в логе — только исход и профиль.

**Город** — ``Tenant.city`` соло-workspace мастера. Без города провайдер не
вызывается вовсе (409 ``no_city``): подсказка «по всей стране» — не место мастера.

**Провайдер** — ``settings.GEOCODING_PROVIDER``; подсказки есть только у DaData
(решение N1). Пустая настройка или пустой ключ — 503 ``misconfigured`` без
запроса в сеть; другой провайдер — 503 ``suggest_not_supported``; лимит, сбой,
сеть, неожиданное исключение провайдера — 503 ``unavailable``. Не 500 ни в
одном из случаев: экран места падает в ручной ввод адреса.

**Журнал §96 не ведётся.** Ввод мастера не хранится, данных о субъекте ручка не
читает; строка передаётся обработчику DaData по решению N1 — эта передача
названа причиной в сторожe журнала (``privacy_audit/tests/test_access_journal.py``).
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from core.geocoding.contract import GeocodeResult, Outcome
from core.geocoding.providers.dadata import DaDataGeocoder
from tenants.models import Tenant
from users.models import SpecialistProfile
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

QUERY_MIN = 3
QUERY_MAX = 300

REASON_NO_WORKSPACE = "no_workspace_tenant"
REASON_SALON = "salon_place_owner_managed"
REASON_NO_CITY = "no_city"
REASON_MISCONFIGURED = "misconfigured"
REASON_NOT_SUPPORTED = "suggest_not_supported"
REASON_UNAVAILABLE = "unavailable"


def _provider() -> DaDataGeocoder:
    """Провайдер подсказок. Отдельной функцией — чтобы тест подставил сессию без сети."""
    return DaDataGeocoder()


def _invalid(details: dict) -> Response:
    return error_response(
        ErrorCode.VALIDATION_ERROR, "Invalid address suggest request.", details=details,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _refused(reason: str) -> Response:
    return error_response(
        ErrorCode.CONFLICT, "Address suggestions are not available for this workspace.",
        details={"reason": reason}, status_code=status.HTTP_409_CONFLICT,
    )


def _unavailable(reason: str) -> Response:
    return error_response(
        ErrorCode.SERVICE_UNAVAILABLE, "Address suggestions are unavailable, enter the address manually.",
        details={"reason": reason}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class InternalAddressSuggestView(APIView):
    """См. докстринг модуля."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"
    http_method_names = ["post"]

    @extend_schema(
        operation_id="internal_specialist_address_suggest",
        tags=["internal"],
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiResponse(description="q is missing, too short or too long"),
            403: OpenApiResponse(description="Not the acting subject"),
            404: OpenApiResponse(description="Specialist not found"),
            409: OpenApiResponse(description="Not a solo workspace, or the workspace has no city"),
            503: OpenApiResponse(description="Suggestions unavailable — enter the address manually"),
        },
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = SpecialistProfile.objects.select_related("tenant").filter(pk=specialist_id).first()
        if profile is None:
            return error_response(
                ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=status.HTTP_404_NOT_FOUND,
            )
        tenant = profile.tenant
        if tenant is None:
            return _refused(REASON_NO_WORKSPACE)
        if tenant.kind != Tenant.Kind.SOLO:
            return _refused(REASON_SALON)

        data = request.data if isinstance(request.data, dict) else None
        if data is None:
            return _invalid({"body": 'a JSON object {"q": "..."} is expected'})
        unknown = sorted(set(data) - {"q"})
        if unknown:
            return _invalid({"unknown_fields": unknown})
        q = data.get("q")
        query = q.strip() if isinstance(q, str) else ""
        if not QUERY_MIN <= len(query) <= QUERY_MAX:
            return _invalid({"q": f"from {QUERY_MIN} to {QUERY_MAX} characters"})

        city = (tenant.city or "").strip()
        if not city:
            return _refused(REASON_NO_CITY)

        provider_name = settings.GEOCODING_PROVIDER
        if not provider_name:
            logger.warning("internal.address_suggest.refused specialist=%s reason=%s", profile.pk, REASON_MISCONFIGURED)
            return _unavailable(REASON_MISCONFIGURED)
        if provider_name != DaDataGeocoder.name:
            return _unavailable(REASON_NOT_SUPPORTED)

        try:
            got = _provider().suggest(query, city=city)
        except Exception as exc:  # noqa: BLE001 — сбой провайдера не становится 500; текст исключения может нести ввод
            logger.error(
                "internal.address_suggest.provider_error specialist=%s error=%s", profile.pk, type(exc).__name__,
            )
            return _unavailable(REASON_UNAVAILABLE)

        if isinstance(got, GeocodeResult):
            if got.outcome is Outcome.NOT_FOUND:
                return success_response({"city": city, "suggestions": []})
            reason = REASON_MISCONFIGURED if got.outcome is Outcome.MISCONFIGURED else REASON_UNAVAILABLE
            logger.warning(
                "internal.address_suggest.unavailable specialist=%s outcome=%s", profile.pk, got.outcome.value,
            )
            return _unavailable(reason)

        return success_response({
            "city": city,
            "suggestions": [
                {"value": item.value, "unrestricted_value": item.unrestricted_value} for item in got
            ],
        })
