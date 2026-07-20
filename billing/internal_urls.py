"""URL routes for /api/v1/internal/billing/ — mounted by W1's urls patch."""
from __future__ import annotations

from django.urls import path

from billing.internal_api import (
    BillingCardSetupView,
    BillingPayDebtView,
    BillingSpecialistStatusView,
)


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
    path(
        "specialists/<uuid:specialist_id>/pay-debt/",
        BillingPayDebtView.as_view(),
        name="internal-billing-pay-debt",
    ),
]
