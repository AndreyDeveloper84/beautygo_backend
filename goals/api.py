"""Internal bot-service API for the goal layer (DRF-1190).

Surface under /api/v1/internal/me/ — same auth pattern as the #97/#99
endpoints: Bearer service token + X-External-User-ID resolved into
``request.user`` by ``IsBotServiceWithVerifiedClient``.

- ``GET  decision-context/`` — эфемерный документ состояния (сердце
  проекции: экран-отрисовщик ничего не вычисляет сам).
- ``POST goals/select/`` — зафиксировать выбор (goal_key XOR goal_text)
  или намерение ``need_guidance``; ответ — обновлённый документ
  состояния (цикл проекции: уточнение — не тупик, а перерисовка).
"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import success_response

from .decision_context import INTENT_NEED_GUIDANCE, build_decision_context
from .models import ClientGoal

logger = logging.getLogger(__name__)


class GoalSelectSerializer(serializers.Serializer):
    """Ровно одно из: goal_key / goal_text / intent=need_guidance."""

    goal_key = serializers.SlugField(max_length=64, required=False)
    goal_text = serializers.CharField(max_length=1000, required=False, allow_blank=False)
    intent = serializers.ChoiceField(
        choices=[INTENT_NEED_GUIDANCE], required=False,
    )
    source_channel = serializers.ChoiceField(
        choices=ClientGoal.SourceChannel.choices, required=True,
    )

    def validate(self, attrs):
        provided = [
            bool(attrs.get("goal_key")),
            bool(attrs.get("goal_text")),
            bool(attrs.get("intent")),
        ]
        if sum(provided) != 1:
            raise serializers.ValidationError(
                "Provide exactly one of: goal_key, goal_text, intent."
            )
        if (attrs.get("goal_key") or attrs.get("goal_text")) and not attrs.get(
            "source_channel"
        ):
            raise serializers.ValidationError(
                {"source_channel": "Required when selecting a goal."}
            )
        return attrs


def _emit_goal_selected(*, client, goal: ClientGoal) -> None:
    """Событие воронки goal_selected. Эмиссия серверная: клиентских
    client_event_id у нас нет — генерируем; повторная запись той же цели
    = новая строка (смена цели и есть событие)."""
    AnalyticsEvent.objects.create(
        event_name=event_catalogue.GOAL_SELECTED,
        payload={
            "goal_key": goal.goal_key,
            "has_text": bool(goal.goal_text),
            "source_channel": goal.source_channel,
        },
        actor=client,
        app_type=AnalyticsEvent.AppType.CLIENT,
        client_event_id=uuid.uuid4(),
    )


class DecisionContextView(APIView):
    """GET /api/v1/internal/me/decision-context/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Decision context document"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def get(self, request: Request) -> Response:
        return success_response(build_decision_context(request.user))


class GoalSelectView(APIView):
    """POST /api/v1/internal/me/goals/select/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        request=GoalSelectSerializer,
        responses={
            200: OpenApiResponse(description="Updated decision context document"),
            400: OpenApiResponse(description="Validation error"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = GoalSelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("intent") == INTENT_NEED_GUIDANCE:
            # «Не понимаю, чего хочу» — состояние ведения, не выход и не
            # цель: ClientGoal не создаём, событие не эмитим, возвращаем
            # документ с первым ведущим вопросом (Ответ 3).
            logger.info("goals.need_guidance user_id=%s", request.user.id)
            return success_response(
                build_decision_context(request.user, guidance=True)
            )

        with transaction.atomic():
            # Одна активная цель: прежняя закрывается, новая создаётся.
            ClientGoal.objects.filter(client=request.user, is_active=True).update(
                is_active=False
            )
            goal = ClientGoal.objects.create(
                client=request.user,
                goal_key=data.get("goal_key") or None,
                goal_text=(data.get("goal_text") or "").strip() or None,
                source_channel=data["source_channel"],
            )
        _emit_goal_selected(client=request.user, goal=goal)

        logger.info(
            "goals.selected user_id=%s goal_key=%r has_text=%s channel=%s",
            request.user.id, goal.goal_key, bool(goal.goal_text),
            goal.source_channel,
        )
        return success_response(build_decision_context(request.user))
