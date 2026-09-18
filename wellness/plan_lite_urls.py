"""DRF-2101 — Plan Lite writer routes (mounted at /api/v1/internal/me/plan-lite/)."""
from django.urls import path

from .plan_lite_api import PlanLiteView

urlpatterns = [
    path("", PlanLiteView.as_view(), name="me-plan-lite"),
]
