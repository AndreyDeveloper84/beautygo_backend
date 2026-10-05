"""POST /api/v1/internal/me/consent-events/ — смена согласия от бота (DRF-2776).

Контракт согласован с бот-стороной (DRF-2776, окно ayla-8d) 05.10:

* ``Authorization: Bearer {AYLA_INTERNAL_API_TOKEN}`` +
  ``X-External-User-ID: bot:{channel}:{channel_user_id}``. Субъект — ТОЛЬКО
  из заголовка и разрешается **без создания** пользователя
  (``resolve_external_user_readonly``): ручка, заводящая строку человека ради
  удаления его данных, создавала бы данные о человеке.
* Тело: ``{event_id, consent_type, granted, granted_at, granted_via?}``.
* Ответ — всегда ``200`` с ``{"data": {event_id, outcome, erased}}``, где
  ``outcome`` ∈ applied / duplicate / ignored / stale / no_subject.
  ``erased`` — ИМЕНА стёртых полей, без значений. Неизвестный человек,
  неизвестный тип и повтор — тоже успех: иначе бот складывал бы такие события
  в DLQ навсегда.
* ``400`` — форма тела; ``403`` — токен или заголовок.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.consent_events import ConsentEvent, apply_consent_event
from users.permissions import IsInternalBearer
from users.response import error_response, success_response
from users.services import is_valid_external_user_id, resolve_external_user_readonly


class ConsentEventSerializer(serializers.Serializer):
    event_id = serializers.CharField(min_length=8, max_length=64)
    consent_type = serializers.CharField(min_length=1, max_length=40)
    granted = serializers.BooleanField()
    granted_at = serializers.DateTimeField()
    granted_via = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")

    def validate_granted(self, value):
        # ``BooleanField`` молча принимает «false», 0 и «no» — в отзыве
        # согласия это место, где опечатка вызывающего не должна читаться
        # ни как да, ни как нет.
        if not isinstance(self.initial_data.get("granted"), bool):
            raise serializers.ValidationError("granted must be a JSON boolean")
        return value

    def validate_granted_at(self, value):
        raw = self.initial_data.get("granted_at") or ""
        if isinstance(raw, str) and not (raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]):
            raise serializers.ValidationError("granted_at must carry a timezone")
        return value


class ConsentEventView(APIView):
    """POST /api/v1/internal/me/consent-events/"""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    # Своё ведро, а не общее анонимное (30/мин на адрес): бот доставляет со
    # своего одного адреса и разбирает накопившееся пачкой. 429 бот читает
    # как временный отказ — согласовано.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "consent_events_internal"

    @extend_schema(
        tags=["internal"],
        request=ConsentEventSerializer,
        responses={
            200: OpenApiResponse(description="Applied (outcome in body)"),
            400: OpenApiResponse(description="Malformed body"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def post(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        if not is_valid_external_user_id(external_user_id):
            return error_response(
                ErrorCode.PERMISSION_DENIED, "X-External-User-ID is required", status_code=403,
            )
        serializer = ConsentEventSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                ErrorCode.VALIDATION_ERROR, "invalid consent event",
                details=serializer.errors, status_code=400,
            )
        data = serializer.validated_data
        user = resolve_external_user_readonly(external_user_id)
        applied = apply_consent_event(
            user,
            ConsentEvent(
                event_id=data["event_id"],
                consent_type=data["consent_type"],
                granted=data["granted"],
                granted_at=data["granted_at"],
                granted_via=data.get("granted_via", ""),
            ),
        )
        return success_response(applied.as_payload())
