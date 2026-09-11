"""URL conf for /api/v1/internal/me/goals/state/ (DRF-1660)."""
from django.urls import path

from .api import GoalStateView

urlpatterns = [
    path("", GoalStateView.as_view(), name="me-goal-state"),
]
