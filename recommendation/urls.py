"""URL-conf границы резолвера — `POST /api/v1/internal/recommendation/resolve/`.

Ручка одна. Их не станет две: вторая ручка — это второе место, где можно
получить порядок, а значит второй авторитет через месяц.
"""
from django.urls import path

from .views import RecommendationResolveView


urlpatterns = [
    path("resolve/", RecommendationResolveView.as_view(), name="recommendation-resolve"),
]
