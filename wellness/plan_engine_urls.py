"""DRF-2857 — durable Plan routes (mounted at /api/v1/internal/me/plan/)."""
from django.urls import path

from .plan_engine_api import PlanEngineStateView, PlanEngineView

urlpatterns = [
    path("", PlanEngineView.as_view(), name="me-plan"),
    path("state/", PlanEngineStateView.as_view(), name="me-plan-state"),
]
