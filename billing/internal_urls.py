"""URL routes for /api/v1/internal/billing/ — mounted by W1's urls patch."""
from __future__ import annotations

from django.urls import path

from billing.internal_api import BillingCardSetupView, BillingSpecialistStatusView


urlpatterns = [
    path(
        "specialists/<uuid:specialist_id>/status/",
        BillingSpecialistStatusView.as_view(),
        name="internal-billing-specialist-status",
    ),
    path(
        "specialists/<uuid:specialist_id>/card-setup/",
        BillingCardSetupView.as_view(),
        name="internal-billing-card-setup",
    ),
]
