"""URL conf for /api/v1/internal/me/goals/select/ (DRF-1190)."""
from django.urls import path

from .api import GoalSelectView

urlpatterns = [
    path("", GoalSelectView.as_view(), name="me-goal-select"),
]
