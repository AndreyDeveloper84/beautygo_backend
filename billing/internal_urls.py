"""URL routes for /api/v1/internal/billing/ — mounted by W1's urls patch."""
from __future__ import annotations

from django.urls import path

from billing.internal_api import BillingSpecialistStatusView


urlpatterns = [
    path(
        "specialists/<uuid:specialist_id>/status/",
        BillingSpecialistStatusView.as_view(),
        name="internal-billing-specialist-status",
    ),
]
