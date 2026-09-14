"""Заявки мастера о разрыве канона — внутренние ручки для бота (M9, DRF-1801).

* ``GET  /api/v1/internal/specialists/{specialist_id}/canon-gap-requests/`` — свои заявки;
* ``POST …/canon-gap-requests/`` — завести ``PENDING`` (в ответе — похожий канон, без связи);
* ``GET  …/canon-gap-requests/similar/?name=`` — подсказка «похожая услуга», только чтение;
* ``GET  …/canon-gap-requests/{request_id}/`` — одна своя заявка.

Сторож — :class:`IsInternalBearerForSpecialistSubject`, как у часов мастера
(DRF-1815): runtime-Bearer, названный субъект, LINKED, профиль в URL —
профиль этого актора. До pre-LINKED-принципала (M28) неприв­язанная
прокси получает 403 ``subject_unresolved``.

**Решать здесь нельзя.** Ни PATCH, ни PUT, ни DELETE: статус пишет только
владелец в Django-admin (``services.canon_gap.decide``, §143). Бот и LLM
заявку заводят и читают — и всё.
"""

from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from services.canon_gap import as_payload, create_request, similar_templates
from services.models import CanonGapRequest
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

logger = logging.getLogger(__name__)


class CanonGapRequestCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    duration_minutes = serializers.IntegerField(min_value=1, max_value=24 * 60)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)


def _profile(specialist_id: UUID):
    from users.models import SpecialistProfile

    return SpecialistProfile.objects.filter(pk=specialist_id).select_related("tenant").first()


def _similar_payload(name: str) -> list[dict]:
    return [
        {"template_id": s.template_id, "name": s.name, "matched_by": s.matched_by}
        for s in similar_templates(name)
    ]


class InternalCanonGapRequestListView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"
    http_method_names = ["get", "post"]

    @extend_schema(tags=["internal"], responses={200: OpenApiResponse(description="requests[]")})
    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return error_response("NOT_FOUND", "Specialist profile not found.", status_code=404)
        rows = CanonGapRequest.objects.filter(specialist=profile).order_by("-created_at")
        return success_response({"requests": [as_payload(r) for r in rows]})

    @extend_schema(
        tags=["internal"],
        request=CanonGapRequestCreateSerializer,
        responses={201: OpenApiResponse(description="request + similar[] (no link)")},
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return error_response("NOT_FOUND", "Specialist profile not found.", status_code=404)
        serializer = CanonGapRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        req = create_request(
            profile,
            name=data["name"],
            description=data.get("description", ""),
            duration_minutes=data["duration_minutes"],
            price=data["price"],
        )
        logger.info("internal.canon_gap.created specialist=%s request=%s", profile.pk, req.pk)
        return success_response(
            {"request": as_payload(req), "similar": _similar_payload(req.name)}, status_code=201
        )


class InternalCanonGapSimilarView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"
    http_method_names = ["get"]

    @extend_schema(tags=["internal"], responses={200: OpenApiResponse(description="similar[]")})
    def get(self, request: Request, specialist_id: UUID) -> Response:
        name = (request.query_params.get("name") or "").strip()
        if not name:
            return error_response("VALIDATION_ERROR", "name is required.", status_code=400)
        return success_response({"similar": _similar_payload(name)})


class InternalCanonGapRequestDetailView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"
    http_method_names = ["get"]

    @extend_schema(tags=["internal"], responses={200: OpenApiResponse(description="request")})
    def get(self, request: Request, specialist_id: UUID, request_id: UUID) -> Response:
        req = CanonGapRequest.objects.filter(pk=request_id, specialist_id=specialist_id).first()
        if req is None:
            # Чужая заявка неотличима от несуществующей.
            return error_response("NOT_FOUND", "Canon gap request not found.", status_code=404)
        return success_response({"request": as_payload(req)})
