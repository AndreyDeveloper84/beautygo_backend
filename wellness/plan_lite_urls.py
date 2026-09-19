"""DRF-2101 / DRF-2123 — Plan Lite routes (mounted at /api/v1/internal/me/plan-lite/)."""
from django.urls import path

from .plan_lite_api import PlanLiteProposalView, PlanLiteView

urlpatterns = [
    path("", PlanLiteView.as_view(), name="me-plan-lite"),
    path("proposal/", PlanLiteProposalView.as_view(), name="me-plan-lite-proposal"),
]
