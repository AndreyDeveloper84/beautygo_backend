"""URL routes for /api/v1/internal/billing/ — mounted by W1's urls patch."""
from __future__ import annotations

from django.urls import path

from billing.internal_api import (
    BillingCardSetupView,
    BillingPayDebtView,
    BillingSpecialistStatusView,
)
from billing.internal_tenant_api import (
    TenantBillingStatusView,
    TenantCancelView,
    TenantCardSetupView,
    TenantConvertToSalonView,
    TenantPayDebtView,
    TenantSubscriptionView,
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
    # DRF-2984 C3 — the cabinet account (contract §6).
    path(
        "tenants/<uuid:tenant_id>/subscription/",
        TenantSubscriptionView.as_view(),
        name="internal-billing-tenant-subscription",
    ),
    path(
        "tenants/<uuid:tenant_id>/status/",
        TenantBillingStatusView.as_view(),
        name="internal-billing-tenant-status",
    ),
    path(
        "tenants/<uuid:tenant_id>/card-setup/",
        TenantCardSetupView.as_view(),
        name="internal-billing-tenant-card-setup",
    ),
    path(
        "tenants/<uuid:tenant_id>/pay-debt/",
        TenantPayDebtView.as_view(),
        name="internal-billing-tenant-pay-debt",
    ),
    path(
        "tenants/<uuid:tenant_id>/cancel/",
        TenantCancelView.as_view(),
        name="internal-billing-tenant-cancel",
    ),
    path(
        "tenants/<uuid:tenant_id>/convert-to-salon/",
        TenantConvertToSalonView.as_view(),
        name="internal-billing-tenant-convert-to-salon",
    ),
]
