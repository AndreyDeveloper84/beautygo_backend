"""Internal bot-service API for the wellness read layer (DRF-1344).

Surface under /api/v1/internal/me/ — тот же auth-паттерн, что у
goals/#97/#99: Bearer service token + X-External-User-ID, разрешённый в
``request.user`` через ``IsBotServiceWithVerifiedClient``.

- ``GET wellness-context/`` — эфемерный документ состояний (только коды)
  для решающего слоя бота. Fail-closed через гейты `wellness/services.py`.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsBotServiceWithVerifiedClient
from users.response import success_response

from .context_read import build_wellness_context


class WellnessContextView(APIView):
    """GET /api/v1/internal/me/wellness-context/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Wellness context document"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def get(self, request: Request) -> Response:
        return success_response(build_wellness_context(request.user))
