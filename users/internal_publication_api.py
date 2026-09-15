"""Публикация соло-мастера под субъектом (DRF-1796, M4; макеты 8.1–8.4).

* ``GET  /api/v1/internal/specialists/{id}/publication/readiness/`` — готов /
  не готов с поимённым списком недостающего;
* ``POST /api/v1/internal/specialists/{id}/publication/`` ``{"command_id"}`` —
  «Опубликовать»: DRAFT → PENDING, идемпотентно по ключу команды;
* ``GET  /api/v1/internal/specialists/{id}/publication/status/`` — «Проверить
  статус»: статус профиля, готовность, последняя команда.

Сторож — :class:`IsInternalBearerForSpecialistSubject`: связанный мастер или
pre-LINKED владелец своего DRAFT workspace (читать готовность он может,
опубликоваться — нет: ``identity_not_linked`` в той же готовности). Логика —
``users/publication.py``.
"""

from __future__ import annotations

from uuid import UUID

from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsInternalBearerForSpecialistSubject
from users.publication import (
    PublicationNotReady,
    PublicationRefused,
    publication_readiness,
    publication_status,
    publish,
    request_as_dict,
)
from users.response import error_response, success_response


class _PublishBody(serializers.Serializer):
    command_id = serializers.UUIDField()


def _profile(specialist_id: UUID):
    from users.models import SpecialistProfile

    return (
        SpecialistProfile.objects
        .select_related("tenant", "works_at", "user")
        .filter(pk=specialist_id)
        .first()
    )


def _no_specialist() -> Response:
    return error_response(
        ErrorCode.SPECIALIST_NOT_FOUND, "Specialist not found.", status_code=404,
    )


def _refused(exc: PublicationRefused) -> Response:
    return error_response(
        ErrorCode.PUBLICATION_REFUSED,
        "Publication is not available for this workspace.",
        details={"reason": exc.reason},
        status_code=status.HTTP_409_CONFLICT,
    )


class _SubjectView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSpecialistSubject]
    subject_url_kwarg = "specialist_id"


class InternalSpecialistPublicationReadinessView(_SubjectView):
    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return _no_specialist()
        try:
            readiness = publication_readiness(profile)
        except PublicationRefused as exc:
            return _refused(exc)
        return success_response({"specialist_id": str(profile.pk), **readiness.as_dict()})


class InternalSpecialistPublicationView(_SubjectView):
    def post(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return _no_specialist()
        body = _PublishBody(data=request.data)
        if not body.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR,
                "command_id (UUID) is required.",
                details=body.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = publish(profile, body.validated_data["command_id"])
        except PublicationRefused as exc:
            return _refused(exc)
        except PublicationNotReady as exc:
            return error_response(
                ErrorCode.PUBLICATION_NOT_READY,
                "Workspace is not ready for publication.",
                details=exc.readiness.as_dict(),
                status_code=status.HTTP_409_CONFLICT,
            )
        submitted = (
            not result.replayed
            and result.request.outcome == result.request.Outcome.SUBMITTED
        )
        return success_response(
            {
                "specialist_id": str(profile.pk),
                "profile_status": result.request.to_status,
                "replayed": result.replayed,
                "request": request_as_dict(result.request),
            },
            status_code=status.HTTP_201_CREATED if submitted else status.HTTP_200_OK,
        )


class InternalSpecialistPublicationStatusView(_SubjectView):
    def get(self, request: Request, specialist_id: UUID) -> Response:
        profile = _profile(specialist_id)
        if profile is None:
            return _no_specialist()
        try:
            return success_response(publication_status(profile))
        except PublicationRefused as exc:
            return _refused(exc)
