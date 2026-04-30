"""URL routes for the nutrition API.

Mounted at /api/v1/nutrition/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from nutrition.views import (
    FoodLogCreateView,
    FoodScanView,
    InternalFoodLogView,
    InternalFoodScanView,
    InternalSummaryView,
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
    # Service-to-service endpoints (DRF-246/247) — auth via X-Service-Token
    # + X-External-User-ID. Used by the MAX bot.
    path(
        "internal/scan/",
        InternalFoodScanView.as_view(),
        name="internal-food-scan",
    ),
    path(
        "internal/food-log/",
        InternalFoodLogView.as_view(),
        name="internal-food-log",
    ),
    path(
        "internal/summary/",
        InternalSummaryView.as_view(),
        name="internal-nutrition-summary",
    ),
]
