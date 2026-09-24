"""``POST /api/v1/internal/specialists/<uuid>/identity/`` — DRF-2442.

Дверь одной силы: MAX-личность мастера, ПРИНЯВШЕГО одноразовое приглашение,
связывается с его уже существующей учёткой специалиста. Под собственным
credential (:class:`users.permissions.IsSpecialistIdentityLinkBearer`). Логика —
в :mod:`users.specialist_identity_linking`; здесь форма запроса, статус по
причине и лимит.

Вызывающий — бот, сразу после ``master_api.views.onboarding_accept``, где
приглашение уже погашено. Доказательство владения — это гашение, а не «бот
сказал»: ограничение s2s-ручки ``bind-external`` («no bot-driven binding until a
verified ownership flow exists») здесь не снимается, а удовлетворяется
(решение владельца §77 п.38, 24.09.2026).

### Ответы

* ``201`` — связь создана и подтверждена readback'ом боевого пути сторожа;
  ``200`` — повтор (тот же ключ, то же тело) либо личность уже связана с этим
  же специалистом;
* ``400 VALIDATION_ERROR`` — форма; ``400 SPECIALIST_IDENTITY_LINK_REFUSED
  invalid_external_user_id``;
* ``403`` — сторож: нет / чужой / пустой credential. **Это отдельный отказ**, не
  путать с «не тот субъект» ниже: «нас не пустили» и «мы просим не про того» —
  разные утверждения, и разное лечение;
* ``404`` — ``specialist_not_found`` (такого профиля нет) ·
  ``identity_unknown`` (бот ещё не предъявлял каталогу эту личность — связывать
  нечего, §148);
* ``409`` — FAIL_CLOSED, ничего не записано: ``specialist_not_linkable``
  (профиль есть, но его учётка не годится в цель — не ``specialist``, прокси,
  выключена, удалена, салон выключен) · ``identity_not_proxy`` ·
  ``identity_already_bound`` (связана с ДРУГОЙ учёткой — перепривязка здесь не
  делается) · ``idempotency_key_reused`` · ``bind_refused``;
* ``429`` — лимит ``specialist_identity_link``;
* ``500 SPECIALIST_IDENTITY_LINK_REFUSED readback_failed`` — значение записано,
  а кабинет не открылся; повтор с тем же ключом повторит readback.
"""

from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsSpecialistIdentityLinkBearer
from users.response import error_response, success_response
from users.specialist_identity_linking import (
    SpecialistIdentityLinkRefused,
    link_specialist_identity,
)

logger = logging.getLogger(__name__)


class _SpecialistIdentityLinkRequestSerializer(serializers.Serializer):
    external_user_id = serializers.CharField(max_length=200)
    actor = serializers.CharField(max_length=200)
    correlation_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="",
    )
    idempotency_key = serializers.CharField(max_length=64, min_length=8)


class _SpecialistIdentityLinkResponseSerializer(serializers.Serializer):
    specialist_id = serializers.UUIDField()
    ayla_user_id = serializers.UUIDField()
    status = serializers.CharField()


class InternalSpecialistIdentityLinkView(APIView):
    """One capability, its own credential (owner ruling §77 п.38): see module."""

    authentication_classes: list = []
    permission_classes = [IsSpecialistIdentityLinkBearer]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "specialist_identity_link"
    serializer_class = _SpecialistIdentityLinkRequestSerializer

    @extend_schema(
        operation_id="internal_specialist_identity_link",
        tags=["internal"],
        request=_SpecialistIdentityLinkRequestSerializer,
        responses={
            200: _SpecialistIdentityLinkResponseSerializer,
            201: _SpecialistIdentityLinkResponseSerializer,
            400: OpenApiResponse(description="Malformed body / invalid external_user_id"),
            403: OpenApiResponse(
                description="Missing / invalid specialist-identity-link bearer (the general, "
                            "both provisioning and the salon-admin-link tokens are NOT accepted)",
            ),
            404: OpenApiResponse(
                description="specialist_not_found · identity_unknown (details.reason)",
            ),
            409: OpenApiResponse(
                description="FAIL_CLOSED: specialist_not_linkable, identity_not_proxy, "
                            "identity_already_bound, idempotency_key_reused, bind_refused "
                            "(details.reason)",
            ),
            429: OpenApiResponse(description="specialist_identity_link rate limit"),
        },
        description=(
            "DRF-2442 (owner ruling §77 п.38, 24.09): bind the MAX identity of a master "
            "who ACCEPTED a one-time invitation to the specialist account that already "
            "exists. Creates nothing — no account, no profile, no relationship — and "
            "accepts role=specialist only. SUCCESS only after the live subject gate "
            "confirms the workspace opened; a bound identity, an unknown identity or a "
            "reused key is refused by name."
        ),
    )
    def post(self, request: Request, specialist_id: UUID) -> Response:
        serializer = _SpecialistIdentityLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            link = link_specialist_identity(
                specialist_id,
                data["external_user_id"],
                actor=data["actor"],
                idempotency_key=data["idempotency_key"],
                correlation_id=data["correlation_id"],
            )
        except SpecialistIdentityLinkRefused as exc:
            return error_response(
                ErrorCode.SPECIALIST_IDENTITY_LINK_REFUSED,
                "Specialist identity not linked.",
                details={"reason": exc.reason},
                status_code=exc.status_code,
            )

        return success_response(
            {
                "specialist_id": str(link.profile.pk),
                "ayla_user_id": str(link.user.pk),
                "status": "created" if link.created else "replayed",
            },
            status_code=status.HTTP_201_CREATED if link.created else status.HTTP_200_OK,
        )


__all__ = ["InternalSpecialistIdentityLinkView"]
