"""URL routes for the nutrition API.

Mounted at /api/v1/nutrition/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from nutrition.views import (
    FoodLogCreateView,
    FoodScanView,
    InternalBeveragesView,
    InternalCrossDomainConvertView,
    InternalCrossDomainDismissView,
    InternalCrossDomainHistoryView,
    InternalCrossDomainSeenView,
    InternalCrossDomainView,
    InternalDeficitsView,
    InternalFoodLogView,
    InternalFoodScanView,
    InternalPatternsView,
    InternalProfileView,
    InternalReturningSuccessView,
    InternalSummaryView,
    InternalWaterCreateView,
    InternalWaterDeleteView,
    InternalWaterRestoreView,
    InternalWaterTodayView,
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
    path(
        "internal/deficits/",
        InternalDeficitsView.as_view(),
        name="internal-nutrition-deficits",
    ),
    path(
        "internal/beverages/",
        InternalBeveragesView.as_view(),
        name="internal-beverages",
    ),
    # Phase 3 nutrition profile (DRF-300)
    path(
        "internal/profile/",
        InternalProfileView.as_view(),
        name="internal-nutrition-profile",
    ),
    # Phase 3.3 pattern detection (DRF-304)
    path(
        "internal/patterns/",
        InternalPatternsView.as_view(),
        name="internal-nutrition-patterns",
    ),
    # Phase 3.3 returning-success insight (DRF-305)
    path(
        "internal/insights/returning_success/",
        InternalReturningSuccessView.as_view(),
        name="internal-returning-success",
    ),
    # Phase 3 water tracker (DRF-302). The /today/ route must precede
    # /<uuid>/ so the resolver doesn't try to coerce "today" to UUID.
    path(
        "internal/water/today/",
        InternalWaterTodayView.as_view(),
        name="internal-water-today",
    ),
    path(
        "internal/water/",
        InternalWaterCreateView.as_view(),
        name="internal-water-create",
    ),
    path(
        "internal/water/<uuid:pk>/restore/",
        InternalWaterRestoreView.as_view(),
        name="internal-water-restore",
    ),
    path(
        "internal/water/<uuid:pk>/",
        InternalWaterDeleteView.as_view(),
        name="internal-water-delete",
    ),
    # DRF-267 — cross-domain insight endpoints. The /history/ route
    # must precede /<uuid:shown_id>/ subresource routes so the resolver
    # doesn't try to coerce "history" to UUID (same pattern as water/today).
    path(
        "internal/insights/cross_domain/history/",
        InternalCrossDomainHistoryView.as_view(),
        name="internal-cross-domain-history",
    ),
    path(
        "internal/insights/cross_domain/<uuid:shown_id>/seen/",
        InternalCrossDomainSeenView.as_view(),
        name="internal-cross-domain-seen",
    ),
    path(
        "internal/insights/cross_domain/<uuid:shown_id>/dismiss/",
        InternalCrossDomainDismissView.as_view(),
        name="internal-cross-domain-dismiss",
    ),
    path(
        "internal/insights/cross_domain/<uuid:shown_id>/convert/",
        InternalCrossDomainConvertView.as_view(),
        name="internal-cross-domain-convert",
    ),
    path(
        "internal/insights/cross_domain/",
        InternalCrossDomainView.as_view(),
        name="internal-cross-domain",
    ),
]
