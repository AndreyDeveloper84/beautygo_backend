"""URL routes for AI Chat API.

Mounted at /api/v1/ai/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from ai.views import (
    AIChatActionView,
    AIChatView,
    ConversationDetailView,
    ConversationListView,
)


app_name = "ai"


urlpatterns = [
    path("chat/", AIChatView.as_view(), name="chat"),
    path(
        "chat/<uuid:conversation_id>/action/",
        AIChatActionView.as_view(),
        name="chat-action",
    ),
    path(
        "conversations/",
        ConversationListView.as_view(),
        name="conversations-list",
    ),
    path(
        "conversations/<uuid:conversation_id>/",
        ConversationDetailView.as_view(),
        name="conversations-detail",
    ),
]
