"""URL routes for /api/v1/internal/users/ — internal service endpoints."""
from __future__ import annotations

from django.urls import path

from users.internal_users_api import InternalUserProfileView


urlpatterns = [
    path(
        "<uuid:user_id>/",
        InternalUserProfileView.as_view(),
        name="internal-user-profile",
    ),
]
