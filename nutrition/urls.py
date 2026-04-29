"""URL routes for the nutrition API.

Mounted at /api/v1/nutrition/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from nutrition.views import (
    FoodLogCreateView,
    FoodScanView,
    NutritionSummaryView,
    WaterLogCreateView,
    WaterLogDeleteView,
    WaterTodayView,
)


app_name = "nutrition"


urlpatterns = [
    path("scan/", FoodScanView.as_view(), name="food-scan"),
    path("food-log/", FoodLogCreateView.as_view(), name="food-log-create"),
    path("summary/", NutritionSummaryView.as_view(), name="nutrition-summary"),
    # /water/today must precede /water/<uuid> — Django URL resolution
    # is order-sensitive but the UUID converter wouldn't accept "today"
    # anyway; ordering left explicit for the next reader.
    path("water/today/", WaterTodayView.as_view(), name="water-today"),
    path("water/", WaterLogCreateView.as_view(), name="water-create"),
    path("water/<uuid:pk>/", WaterLogDeleteView.as_view(), name="water-delete"),
]
