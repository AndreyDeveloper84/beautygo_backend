"""Внутренние ручки заявки на удаление для бота (§7 свода, DRF-1699, D1).

* ``POST /api/v1/internal/users/{ayla_user_id}/deletion-requests/`` —
  завести (201) или вернуть открытую (200). Тело: ``{"initiator": "bot"}``.
* ``GET  /api/v1/internal/users/{ayla_user_id}/deletion-requests/`` —
  текущая заявка (открытая, иначе последняя завершённая; 404 — не было)
  для профиля, который номера не помнит.
* ``GET  /api/v1/internal/users/{ayla_user_id}/deletion-requests/{request_id}/``
  — состояние одной заявки по номеру.

Сторож — ``IsInternalBearer``: тот же, что у ``…/personal-data/`` (C5.2).
Заявка заводится ботом от имени человека, которого бот уже проверил
(``DELETE_CONFIRMATION_TOKEN`` на его стороне); это не провижининг.

### Что здесь НЕ происходит

Ничего не стирается. Ручка только фиксирует волю человека и выдаёт ему
номер и срок. Стирание — исполнитель (D3), остановка персонализации —
D2. Пока их нет, заявка честно висит в ``DELETION_REQUESTED``, и это
правда, а не заглушка.

### Мягко удалённый пользователь

Заявку можно завести и на строку с ``deleted_at`` — человек мог удалить
аккаунт из приложения раньше, чем нажать в Mini App (DRF-1368, тот же
довод, что у ``_get_user_for_erasure``). Исполнитель увидит, что стирать
нечего, и закроет заявку как COMPLETED; отказать 404 значило бы оставить
человека без номера и срока.
"""

from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.deletion_requests import (
    as_payload,
    current_request_for,
    ensure_deletion_request,
    get_deletion_request,
)
from users.models import User
from users.permissions import IsInternalBearer
from users.response import error_response, success_response

logger = logging.getLogger(__name__)

_INITIATORS = ("bot", "app", "admin")


class _CreateDeletionRequestSerializer(serializers.Serializer):
    initiator = serializers.ChoiceField(choices=_INITIATORS, default="bot")


class _DeletionRequestResponseSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()
    created = serializers.BooleanField(required=False)
    status = serializers.CharField()
    requested_at = serializers.DateTimeField()
    deadline_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)
    is_open = serializers.BooleanField()


def _user_or_none(user_id: UUID) -> User | None:
    return User.objects.filter(pk=user_id).first()


class InternalDeletionRequestCreateView(APIView):
    """См. докстринг модуля."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = _CreateDeletionRequestSerializer

    @extend_schema(
        operation_id="internal_deletion_request_create",
        tags=["internal"],
        request=_CreateDeletionRequestSerializer,
        responses={
            200: _DeletionRequestResponseSerializer,
            201: _DeletionRequestResponseSerializer,
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User does not exist"),
        },
        description=(
            "152-ФЗ / owner §7: open a DeletionRequest for the person. "
            "Idempotent — an open request (REQUESTED / PROCESSING / FAILED) "
            "is returned as 200 with the same request_id; otherwise a new "
            "one is created (201) with deadline_at = now + 30 days. Nothing "
            "is erased here: erasure is the executor's job (D3)."
        ),
    )
    @extend_schema(
        operation_id="internal_deletion_request_current",
        tags=["internal"],
        request=None,
        responses={
            200: _DeletionRequestResponseSerializer,
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User does not exist or never asked for deletion"),
        },
        description="The person's current deletion request: the open one, else the latest completed one.",
    )
    def get(self, request: Request, user_id: UUID) -> Response:
        user = _user_or_none(user_id)
        if user is None:
            return error_response("NOT_FOUND", "User not found.", status_code=404)
        req = current_request_for(user)
        if req is None:
            return error_response("NOT_FOUND", "No deletion request.", status_code=404)
        return success_response(as_payload(req))

    def post(self, request: Request, user_id: UUID) -> Response:
        user = _user_or_none(user_id)
        if user is None:
            return error_response("NOT_FOUND", "User not found.", status_code=404)

        serializer = _CreateDeletionRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        ensured = ensure_deletion_request(
            user, initiator=serializer.validated_data["initiator"],
        )
        logger.info(
            "deletion_request.%s user_id=%s deletion_request=%s deadline=%s request_id=%s",
            "created" if ensured.created else "existing",
            user_id,
            ensured.request.pk,
            ensured.request.deadline_at.isoformat(),
            getattr(request, "request_id", "-"),
        )
        # ``created`` и в теле, не только кодом: клиент бота отдаёт наверх
        # JSON без статуса, а человеку «принято» и «уже принято» — разное.
        return success_response(
            {**as_payload(ensured.request), "created": ensured.created},
            status_code=status.HTTP_201_CREATED if ensured.created else status.HTTP_200_OK,
        )


class InternalDeletionRequestDetailView(APIView):
    """GET — состояние одной заявки; чужая или несуществующая — 404."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    @extend_schema(
        operation_id="internal_deletion_request_detail",
        tags=["internal"],
        request=None,
        responses={
            200: _DeletionRequestResponseSerializer,
            401: OpenApiResponse(description="Missing / invalid bearer token"),
            404: OpenApiResponse(description="User or request does not exist"),
        },
    )
    def get(self, request: Request, user_id: UUID, request_id: UUID) -> Response:
        user = _user_or_none(user_id)
        if user is None:
            return error_response("NOT_FOUND", "User not found.", status_code=404)
        req = get_deletion_request(user, request_id)
        if req is None:
            return error_response("NOT_FOUND", "Deletion request not found.", status_code=404)
        return success_response(as_payload(req))
