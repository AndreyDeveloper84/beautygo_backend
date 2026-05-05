"""URL routes for the analytics ingestion API.

Mounted at /api/v1/analytics/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from analytics.views import AnalyticsEventView


app_name = "analytics"


urlpatterns = [
    path("event/", AnalyticsEventView.as_view(), name="event-create"),
]
