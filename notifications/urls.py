"""URL routes for the notifications API (Slice N3).

Mounted at /api/v1/notifications/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from .views import (
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
)


app_name = "notifications"


urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    # /read-all/ before /<uuid:pk>/ so the literal route resolves first;
    # UUID converter wouldn't accept "read-all" anyway, but explicit
    # ordering beats relying on Django's converter strictness.
    path("read-all/", NotificationReadAllView.as_view(), name="read-all"),
    path("<uuid:pk>/read/", NotificationReadView.as_view(), name="read"),
]
