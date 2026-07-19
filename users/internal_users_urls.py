"""URL routes for /api/v1/internal/users/ — internal service endpoints."""
from __future__ import annotations

from django.urls import path

from users.internal_personal_context_api import (
    InternalAskEligibilityView,
    InternalMarkAskedView,
    InternalPersonalContextView,
    InternalSkipView,
)
from users.internal_users_api import InternalUserProfileView


urlpatterns = [
    # Personal-context memory API for the bot (A1a) — Bearer, keyed by ayla_user_id.
    path(
        "<uuid:ayla_user_id>/personal-context/",
        InternalPersonalContextView.as_view(),
        name="internal-personal-context",
    ),
    path(
        "<uuid:ayla_user_id>/personal-context/ask-eligibility/",
        InternalAskEligibilityView.as_view(),
        name="internal-personal-context-ask-eligibility",
    ),
    path(
        "<uuid:ayla_user_id>/personal-context/mark-asked/",
        InternalMarkAskedView.as_view(),
        name="internal-personal-context-mark-asked",
    ),
    path(
        "<uuid:ayla_user_id>/personal-context/skip/",
        InternalSkipView.as_view(),
        name="internal-personal-context-skip",
    ),
    path(
        "<uuid:user_id>/",
        InternalUserProfileView.as_view(),
        name="internal-user-profile",
    ),
]
