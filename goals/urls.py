from django.urls import path

from .api import DecisionContextView

urlpatterns = [
    path("", DecisionContextView.as_view(), name="me-decision-context"),
]
