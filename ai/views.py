"""AI Chat API views — thin wrappers over chat_service / action_service.

Endpoint map (per Notion API Spec v2.0 §AI ASSISTANT):

  POST   /api/v1/ai/chat/                          → AIChatView.post
  POST   /api/v1/ai/chat/{conv_id}/action/         → AIChatActionView.post
  GET    /api/v1/ai/conversations/                 → ConversationListView.get
  GET    /api/v1/ai/conversations/{id}/            → ConversationDetailView.get
  DELETE /api/v1/ai/conversations/{id}/            → ConversationDetailView.delete

X-App-Type: client only — pro app blocked at permission layer.

Anonymous (User.is_guest=True) can hit POST /chat/ but NOT the list
endpoint — list is meaningful only for authenticated users with history.
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from users.permissions import IsClientApp
from users.response import error_response, success_response

from ai.application.services.action_service import ActionService
from ai.application.services.chat_service import (
    ChatRequestContext,
    ChatService,
)
from ai.exceptions import (
    AIAnonymousLimitExceeded,
    AIDailyLimitExceeded,
    AIInvalidAction,
    AINotOwner,
    AIRateLimitExceeded,
    AIUnavailable,
)
from ai.models import Conversation, Message
from ai.serializers import (
    ActionRequestSerializer,
    ChatRequestSerializer,
    ConversationDetailSerializer,
    ConversationListItemSerializer,
    MessageSerializer,
)

logger = logging.getLogger(__name__)


class _ConversationsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _rate_limit_response(exc: AIRateLimitExceeded) -> Response:
    return error_response(
        "RATE_LIMITED",
        "Слишком много запросов",
        details={"reason": exc.reason},
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/ai/chat/
# ---------------------------------------------------------------------------


class AIChatView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsClientApp]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai_chat"

    chat_service_class = ChatService

    def post(self, request: Request) -> Response:
        serializer = ChatRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        validated = serializer.validated_data
        ctx = self._build_context(validated.get("context") or {})
        ctx_with_voice = ChatRequestContext(
            location_lat=ctx.location_lat,
            location_lon=ctx.location_lon,
            preferred_date=ctx.preferred_date,
            preferred_time=ctx.preferred_time,
            voice_mode=bool(validated.get("voice_mode")),
        )

        try:
            result = self.chat_service_class().send_message(
                actor=request.user,
                conversation_id=validated.get("conversation_id"),
                message_text=validated["message"],
                request_context=ctx_with_voice,
            )
        except AIUnavailable as exc:
            logger.warning("ai.chat.unavailable: %s", exc)
            return error_response(
                "AI_UNAVAILABLE",
                "AI временно недоступен",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except AIRateLimitExceeded as exc:
            return _rate_limit_response(exc)

        payload = {
            "conversation_id": str(result.conversation_id),
            "message": MessageSerializer(result.message).data,
        }
        if result.action_type:
            payload["action"] = {
                "type": result.action_type,
                "data": result.action_data or {},
            }
        return success_response(payload)

    @staticmethod
    def _build_context(raw: dict) -> ChatRequestContext:
        loc = raw.get("location") or {}
        return ChatRequestContext(
            location_lat=loc.get("lat"),
            location_lon=loc.get("lon"),
            preferred_date=(
                raw["preferred_date"].isoformat()
                if raw.get("preferred_date")
                else None
            ),
            preferred_time=raw.get("preferred_time"),
        )


# ---------------------------------------------------------------------------
# POST /api/v1/ai/chat/{conversation_id}/action/
# ---------------------------------------------------------------------------


class AIChatActionView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsClientApp]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ai_chat"

    action_service_class = ActionService

    def post(self, request: Request, conversation_id: UUID) -> Response:
        conversation = (
            Conversation.objects.filter(id=conversation_id, is_active=True).first()
        )
        if conversation is None:
            return error_response(
                "CONVERSATION_NOT_FOUND",
                "Диалог не найден",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        serializer = ActionRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )
        validated = serializer.validated_data

        try:
            result = self.action_service_class().execute(
                actor=request.user,
                conversation=conversation,
                action_type=validated["action_type"],
                confirmed=validated["confirmed"],
                data=validated.get("data") or {},
            )
        except AINotOwner:
            return error_response(
                "NOT_OWNER",
                "Диалог принадлежит другому пользователю",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        except AIInvalidAction as exc:
            return error_response(
                "INVALID_ACTION_TYPE",
                str(exc) or "Невалидный action_type",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if result.error_code:
            http = (
                status.HTTP_409_CONFLICT
                if result.error_code == "SLOT_NOT_AVAILABLE"
                else status.HTTP_400_BAD_REQUEST
            )
            return error_response(
                result.error_code,
                "Не удалось выполнить действие",
                details=result.error_details,
                status_code=http,
            )

        payload = {"success": result.success, "result": {}}
        if result.appointment_payload is not None:
            payload["result"]["appointment"] = result.appointment_payload
        if result.next_message is not None:
            payload["result"]["next_message"] = MessageSerializer(
                result.next_message
            ).data
        if result.next_action_type:
            payload["result"]["next_action"] = {
                "type": result.next_action_type,
                "data": result.next_action_data or {},
            }
        return success_response(payload)


# ---------------------------------------------------------------------------
# GET /api/v1/ai/conversations/
# ---------------------------------------------------------------------------


class ConversationListView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsClientApp]
    pagination_class = _ConversationsPagination

    def get(self, request: Request) -> Response:
        if getattr(request.user, "is_guest", False):
            return error_response(
                "FORBIDDEN_FOR_ANONYMOUS",
                "История диалогов доступна только зарегистрированным пользователям",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        from django.db.models import Count

        qs = (
            Conversation.objects.filter(user=request.user, is_active=True)
            .annotate(messages_count=Count("messages"))
            .order_by("-last_message_at", "-created_at")
        )

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = ConversationListItemSerializer(page or [], many=True).data
        return success_response(
            {
                "results": data,
                "count": paginator.page.paginator.count if paginator.page else 0,
                "page": paginator.page.number if paginator.page else 1,
                "page_size": paginator.get_page_size(request),
            }
        )


# ---------------------------------------------------------------------------
# GET / DELETE /api/v1/ai/conversations/{id}/
# ---------------------------------------------------------------------------


class ConversationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsClientApp]

    def _get_owned(self, request: Request, conversation_id: UUID):
        conv = Conversation.objects.filter(
            id=conversation_id, user=request.user, is_active=True
        ).first()
        return conv

    def get(self, request: Request, conversation_id: UUID) -> Response:
        conv = self._get_owned(request, conversation_id)
        if conv is None:
            return error_response(
                "CONVERSATION_NOT_FOUND",
                "Диалог не найден",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        data = ConversationDetailSerializer(conv).data
        return success_response(data)

    def delete(self, request: Request, conversation_id: UUID) -> Response:
        conv = self._get_owned(request, conversation_id)
        if conv is None:
            return error_response(
                "CONVERSATION_NOT_FOUND",
                "Диалог не найден",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        conv.is_active = False
        conv.deleted_at = timezone.now()
        conv.save(update_fields=["is_active", "deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
