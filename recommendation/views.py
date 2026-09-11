"""HTTP-проекция границы — `POST /api/v1/internal/recommendation/resolve/`.

Одна из **двух** проекций одной границы (контракт §9.3). Вторая —
внутрипроцессный вызов `recommendation.api.resolve`, которым пользуются
потребители, живущие в этом же процессе. Обе отдают один и тот же
неизменяемый объект; HTTP тут транспорт, а не источник правды.

Представление намеренно тонкое и **ходит через ту же публичную поверхность,
что и все остальные** — `recommendation.api`. Дай мы ему привилегию читать
приватные модули, оно стало бы вторым входом в резолвер, и первый же
«срочный фикс» пошёл бы через него.

Три вещи, которые здесь делаются и которых не было в старой ручке:

1. **Схема на обоих концах.** Вход разбирается схемой, и выход **тоже**
   проверяется схемой перед отправкой. Источник, отдавший форму, которую сам
   не объявлял, обязан узнать об этом первым — а не потребитель, у которого
   она молча не отрисуется.
2. **`resolver_spec_version` в теле.** Потребитель с неизвестной мажорной
   версией обязан вернуть `CONTRACT_VIOLATION`, а не пытаться разобрать.
3. **Ненастроенный источник — это `UNAVAILABLE`, а не пустая выдача.**
   Пустая выдача утверждает «подходящих нет»; мы же не искали.
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import ErrorCode
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response
from users.deletion_requests import deletion_block_for, deletion_refusal

from ._serializers import ResolveRequestSerializer, ResolveResponseSerializer, decision_to_payload
from ._source_binding import CandidateSourceNotConfigured, get_candidate_source
from .api import RecommendationRequest, SafetyState, Surface, resolve

logger = logging.getLogger(__name__)


class RecommendationResolveView(APIView):
    """`POST /api/v1/internal/recommendation/resolve/` — единственная ручка границы."""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = ResolveRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=ResolveRequestSerializer,
        responses={
            200: ResolveResponseSerializer,
            400: OpenApiResponse(description="Запрос не прошёл схему границы"),
            403: OpenApiResponse(description="Bearer / external id неверны"),
            503: OpenApiResponse(description="Источник доменных фактов недоступен — это НЕ пустая выдача"),
        },
    )
    def post(self, request: Request) -> Response:
        # D2 (§7): живая заявка на удаление — рекомендации не считаются.
        # Проверка ДО разбора тела: отказ по воле человека не зависит от
        # формы запроса, и ошибку формы он получать не должен.
        blocked = deletion_block_for(request.user)
        if blocked is not None:
            return deletion_refusal(blocked)
        serializer = ResolveRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            source = get_candidate_source()
        except CandidateSourceNotConfigured as exc:
            # Отдельная ветка и отдельный код: недоступность источника не
            # должна выглядеть как «посмотрели и не нашли». Именно слияние
            # этих двух состояний в одну ветку и стоило нам DEFECT-C-02.
            logger.error("recommendation.resolve.source_unavailable: %s", exc)
            return error_response(
                ErrorCode.SERVICE_UNAVAILABLE,
                "candidate source is not bound — this is unavailability, not an empty result",
                status_code=503,
            )

        decision = resolve(
            RecommendationRequest(
                request_id=serializer.validated_data["request_id"],
                # Кого спрашивают — говорит аутентификация, а не тело запроса.
                subject_ref=str(request.user.id),
                surface=Surface(serializer.validated_data["surface"]),
                scope=serializer.build_scope(),
                need=serializer.build_need(),
                constraints=serializer.build_constraints(),
                safety_state=SafetyState(serializer.validated_data["safety_state"]),
                tie_break_seed=serializer.validated_data.get("tie_break_seed"),
                k=serializer.validated_data["k"],
            ),
            source=source,
        )

        payload = decision_to_payload(decision)
        # Проверка СВОЕГО выхода. Не паранойя: у формы ответа теперь есть
        # владелец (§2.1 C3), и владелец обязан ловить своё нарушение сам —
        # иначе он снова окажется у потребителя в виде пустого экрана.
        outgoing = ResolveResponseSerializer(data=payload)
        outgoing.is_valid(raise_exception=True)

        logger.info(
            "recommendation.resolve request_id=%s surface=%s ordered=%d excluded=%d tiers=%d",
            decision.request_id,
            serializer.validated_data["surface"],
            len(decision.ordered),
            len(decision.excluded),
            len({c.tier for c in decision.ordered}),
        )
        return success_response(payload)
