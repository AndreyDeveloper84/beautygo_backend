"""GET/POST /api/v1/internal/specialists/{specialist_id}/services/selection/ (DRF-1800, M8a).

Экран «Выберите услуги» мастера (макет 3, P15/P18) через бота. Сторож —
:class:`IsInternalBearerForSpecialistSubject`: связанный мастер (LINKED)
или, до связи, владелец своего provisioned DRAFT workspace (M28). Логика —
``services/offer_selection.py``; здесь только субъект, форма входа и ответа.

Ответ один на обе операции — текущее состояние выбора: список и два
счётчика, посчитанные сервером (счётчики экрана только из ответа):
``selected`` = активные выбранные строки, ``configured`` = из них те, у
которых есть предложение мастера с ценой и длительностью.
"""

from __future__ import annotations

from uuid import UUID

from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsInternalBearerForSpecialistSubject
from users.response import error_response, success_response

from .offer_selection import (
    MAX_TEMPLATES_PER_CALL,
    SelectedService,
    SelectionRefused,
    TemplatesNotFound,
    select_templates,
    selected_services,
)


class _SelectBody(serializers.Serializer):
    template_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=MAX_TEMPLATES_PER_CALL,
    )


def _item(entry: SelectedService) -> dict:
    row = entry.salon_service
    offer = entry.offer
    return {
        "salon_service_id": str(row.pk),
        "template_id": str(row.template_id),
        "name": row.name,
        "category_id": str(row.category_id) if row.category_id else None,
        "is_active": row.is_active,
        "mapping_status": row.mapping_status,
        "offer": None if offer is None else {
            "id": str(offer.pk),
            "price": str(offer.price),
            "duration_minutes": offer.resolved_duration(),
            "is_active": offer.is_active,
        },
        "configured": entry.configured,
    }


def _state(profile) -> dict:
    entries = selected_services(profile)
    return {
        "specialist_id": str(profile.pk),
        "tenant_id": str(profile.tenant_id),
        "selected": sum(1 for e in entries if e.salon_service.is_active),
        "configured": sum(1 for e in entries if e.configured),
        "services": [_item(e) for e in entries],
    }


def _refused(exc: SelectionRefused) -> Response:
    return error_response(
        ErrorCode.SERVICE_SELECTION_REFUSED,
        "Service selection is not available for this workspace.",
        details={"reason": exc.reason},
        status_code=status.HTTP_409_CONFLICT,
    )


class InternalSpecialistServiceSelectionView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"

    @staticmethod
    def _profile(specialist_id: UUID):
        from users.models import SpecialistProfile

        return SpecialistProfile.objects.select_related("tenant").filter(pk=specialist_id).first()

    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return error_response(
                ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
            )
        try:
            return success_response(_state(profile))
        except SelectionRefused as exc:
            return _refused(exc)

    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = self._profile(specialist_id)
        if profile is None:
            return error_response(
                ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
            )
        body = _SelectBody(data=request.data)
        if not body.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                "template_ids must be a non-empty list of UUIDs.",
                details=body.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            created = select_templates(profile, body.validated_data["template_ids"])
        except SelectionRefused as exc:
            return _refused(exc)
        except TemplatesNotFound as exc:
            return error_response(
                ErrorCode.NOT_FOUND,
                "Service template not found.",
                details={"reason": "template_not_found", "template_ids": exc.template_ids},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        payload = {**_state(profile), "created": created}
        return success_response(
            payload, status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
