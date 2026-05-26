"""URL conf for /api/v1/internal/me/catalog/recommendations/ — task #99.

W1 booking flow Phase B unblock. Co-located with the rest of the
internal/me/ bot-auth surface introduced by #97 Records endpoints.
"""
from django.urls import path

from .catalog_recommendations_api import CatalogRecommendationsView


urlpatterns = [
    path(
        "",
        CatalogRecommendationsView.as_view(),
        name="me-catalog-recommendations",
    ),
]
