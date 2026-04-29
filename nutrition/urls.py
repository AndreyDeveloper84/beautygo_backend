"""URL routes for the nutrition API.

Mounted at /api/v1/nutrition/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from nutrition.views import (
    FoodLogCreateView,
    FoodScanView,
    NutritionSummaryView,
)


app_name = "nutrition"


urlpatterns = [
    path("scan/", FoodScanView.as_view(), name="food-scan"),
    path("food-log/", FoodLogCreateView.as_view(), name="food-log-create"),
    path("summary/", NutritionSummaryView.as_view(), name="nutrition-summary"),
]
