"""Notification list/read endpoints — Slice N3.

Per Notion API Spec v2.0 §NOTIFICATIONS:

  GET    /notifications/                → paginated list with unread_count
  PATCH  /notifications/{id}/read/      → mark one read, returns the row
  POST   /notifications/read-all/       → mark all unread read, returns count

Client-only — Pro app doesn't show a notification center yet (M5+).
"""
from __future__ import annotations

import logging

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers as drf_serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsClient, IsClientApp
from users.response import error_response, success_response

from .models import Notification
from .serializers import (
    NotificationListItemSerializer,
    NotificationListQuerySerializer,
)


logger = logging.getLogger(__name__)


class _NotificationPagination(PageNumberPagination):
    """Spec defaults: page_size=20, max 100. The mobile UI uses infinite
    scroll, so a generous max keeps initial load fast without inviting
    huge full-table dumps."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _user_notifications(user) -> QuerySet:
    """Owner-scoped queryset. Reused by all three views so the
    is-this-mine check is in exactly one place."""
    return Notification.objects.filter(user=user).order_by("-created_at")


class NotificationListView(APIView):
    """GET /api/v1/notifications/?is_read=&page=&page_size=

    Paginated. Adds ``unread_count`` (across the WHOLE queryset, not
    just the current page) so the mobile badge stays consistent
    regardless of which page is loaded.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    serializer_class = NotificationListItemSerializer

    @extend_schema(
        parameters=[NotificationListQuerySerializer],
        responses={200: inline_serializer(
            name="NotificationListResponse",
            fields={
                "results": NotificationListItemSerializer(many=True),
                "count": drf_serializers.IntegerField(),
                "unread_count": drf_serializers.IntegerField(),
            },
        )},
    )
    def get(self, request: Request) -> Response:
        q = NotificationListQuerySerializer(data=request.query_params)
        if not q.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные параметры запроса",
                details=q.errors,
            )

        qs = _user_notifications(request.user)
        is_read = q.validated_data.get("is_read")
        if is_read is not None:
            qs = qs.filter(is_read=is_read)

        paginator = _NotificationPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        results = NotificationListItemSerializer(page, many=True).data

        # Spec puts ``count`` and ``unread_count`` at the top of the
        # response object, not inside `meta`. We keep `count` from the
        # paginator and add `unread_count` ourselves; both are full-set
        # counts (paginated `results` is the only thing trimmed by page).
        unread_count = (
            _user_notifications(request.user)
            .filter(is_read=False).count()
        )
        body = {
            "results": results,
            "count": paginator.page.paginator.count,
            "unread_count": unread_count,
        }
        return success_response(body, status_code=status.HTTP_200_OK)


class NotificationReadView(APIView):
    """PATCH /api/v1/notifications/{id}/read/ — mark one as read.

    Idempotent: re-marking an already-read notification is a no-op
    (no DB write, just returns the row). 404 on someone else's
    notification to avoid existence leak.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    serializer_class = NotificationListItemSerializer

    @extend_schema(
        request=None,
        responses={200: NotificationListItemSerializer, 404: None},
    )
    def patch(self, request: Request, pk) -> Response:
        try:
            notification = _user_notifications(request.user).get(id=pk)
        except Notification.DoesNotExist:
            return error_response(
                "NOT_FOUND",
                "Уведомление не найдено",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])
        return success_response(
            NotificationListItemSerializer(notification).data,
            status_code=status.HTTP_200_OK,
        )


class NotificationReadAllView(APIView):
    """POST /api/v1/notifications/read-all/ — mark all unread as read.

    Returns ``{ marked_count: N }`` per spec. Bulk update path skips
    the dispatcher's per-row signals — we only mutate is_read so
    nothing downstream reacts to a "mark read" event today.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]

    @extend_schema(
        request=None,
        responses={200: inline_serializer(
            name="NotificationReadAllResponse",
            fields={"marked_count": drf_serializers.IntegerField()},
        )},
    )
    def post(self, request: Request) -> Response:
        marked = (
            _user_notifications(request.user)
            .filter(is_read=False)
            .update(is_read=True)
        )
        return success_response(
            {"marked_count": marked},
            status_code=status.HTTP_200_OK,
        )
