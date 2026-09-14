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
from users.deletion_request_api import (
    InternalDeletionRequestCreateView,
    InternalDeletionRequestDetailView,
)
from users.internal_users_api import (
    InternalBindExternalIdentityView,
    InternalUserProfileView,
)
from recommendation.record_api import (
    InternalRecommendationEventView,
    InternalRecommendationSetView,
)
from users.personal_data_api import (
    InternalPersonalDataDeleteView,
    InternalPersonalDataExportView,
)


urlpatterns = [
    # DRF-1666 — запись Recommendation для C04.1: чтение набора и события
    # взаимодействия, оба под субъектом (DRF-1617). Здесь, а не в
    # recommendation/urls.py: та ручка — граница резолвера и остаётся одна.
    path(
        "<uuid:user_id>/recommendations/<uuid:set_id>/",
        InternalRecommendationSetView.as_view(),
        name="internal-recommendation-set",
    ),
    path(
        "<uuid:user_id>/recommendations/<uuid:recommendation_id>/events/",
        InternalRecommendationEventView.as_view(),
        name="internal-recommendation-events",
    ),
    # E2E-BOT-02B — identity binding. Static segment, declared before the
    # <uuid:...> routes so it can never be swallowed by a uuid converter
    # (it wouldn't anyway — "bind-external" is not a uuid — explicit
    # ordering documents intent).
    path(
        "bind-external/",
        InternalBindExternalIdentityView.as_view(),
        name="internal-users-bind-external",
    ),
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
    # DRF-1699 (§7 свода) — заявка на удаление аккаунта. Заводится ботом
    # ДО любого стирания; стирание — исполнитель (D3), не эти ручки.
    path(
        "<uuid:user_id>/deletion-requests/",
        InternalDeletionRequestCreateView.as_view(),
        name="internal-deletion-request-create",
    ),
    path(
        "<uuid:user_id>/deletion-requests/<uuid:request_id>/",
        InternalDeletionRequestDetailView.as_view(),
        name="internal-deletion-request-detail",
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
