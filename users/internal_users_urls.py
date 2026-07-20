"""URL routes for /api/v1/internal/users/ — internal service endpoints."""
from __future__ import annotations

from django.urls import path

from payments.views import (
    InternalCardDeleteView,
    InternalCardListView,
    InternalCardSetupView,
)
from users.internal_personal_context_api import (
    InternalAskEligibilityView,
    InternalMarkAskedView,
    InternalPersonalContextView,
    InternalSkipView,
)
from users.internal_users_api import InternalUserProfileView
from users.personal_data_api import (
    InternalPersonalDataDeleteView,
    InternalPersonalDataExportView,
)


urlpatterns = [
    # C7.2 — client card binding (payments app owns the views). Placed
    # before the plain <uuid:user_id>/ route for explicitness (Django
    # matches full segments anyway).
    path(
        "<uuid:ayla_user_id>/cards/setup/",
        InternalCardSetupView.as_view(),
        name="internal-cards-setup",
    ),
    path(
        "<uuid:ayla_user_id>/cards/",
        InternalCardListView.as_view(),
        name="internal-cards-list",
    ),
    path(
        "<uuid:ayla_user_id>/cards/<uuid:card_id>/",
        InternalCardDeleteView.as_view(),
        name="internal-cards-delete",
    ),
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
    # C5 (152-ФЗ) — before the plain <uuid:user_id>/ route so the
    # longer path wins by declaration order (it would anyway — Django
    # matches full segments — but explicit ordering documents intent).
    path(
        "<uuid:user_id>/personal-data/export/",
        InternalPersonalDataExportView.as_view(),
        name="internal-personal-data-export",
    ),
    path(
        "<uuid:user_id>/personal-data/",
        InternalPersonalDataDeleteView.as_view(),
        name="internal-personal-data-delete",
    ),
    path(
        "<uuid:user_id>/",
        InternalUserProfileView.as_view(),
        name="internal-user-profile",
    ),
]
