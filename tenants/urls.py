"""URL conf for /api/v1/tenants/* — #246 Q1+ admin endpoints.

Currently only the revoke endpoint. Adding more admin endpoints
(roster management, cross-tenant transfer, etc.) lands here as the
salon-admin surface grows.
"""
from django.urls import path

from .relationships_admin_api import TenantRelationshipRevokeView


urlpatterns = [
    path(
        "me/relationships/<uuid:user_id>/revoke/",
        TenantRelationshipRevokeView.as_view(),
        name="tenants-me-relationships-revoke",
    ),
]
