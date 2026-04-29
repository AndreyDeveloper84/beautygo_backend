"""URL routes for the favorites API.

Mounted at /api/v1/favorites/ via djangoProject/urls.py.
"""
from __future__ import annotations

from django.urls import path

from .favorites_api import FavoriteAddRemoveView, FavoriteListView


app_name = "favorites"


urlpatterns = [
    path("specialists/", FavoriteListView.as_view(), name="list"),
    path(
        "specialists/<uuid:pk>/",
        FavoriteAddRemoveView.as_view(),
        name="add-remove",
    ),
]
