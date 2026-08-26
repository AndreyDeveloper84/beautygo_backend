"""URL conf for /api/v1/internal/me/wellness-context/ (DRF-1344)."""
from django.urls import path

from .api import WellnessContextView

urlpatterns = [
    path("", WellnessContextView.as_view(), name="me-wellness-context"),
]
