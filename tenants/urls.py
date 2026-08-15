"""URL conf for /api/v1/tenants/* — #246 Q1+ admin endpoints.

The salon-admin surface: everything an administrator does to their own
salon rather than to their own profile. Started with access revocation;
DRF-1063 adds the day journal, which is the first endpoint in this
system where a salon employee can see the work of masters other than
themselves.
"""
from django.urls import path

from .day_api import TenantDayView
from .relationships_admin_api import TenantRelationshipRevokeView


urlpatterns = [
    path(
        "me/relationships/<uuid:user_id>/revoke/",
        TenantRelationshipRevokeView.as_view(),
        name="tenants-me-relationships-revoke",
    ),
    # DRF-1063 — the salon's day: masters, hours, breaks, absences and
    # bookings for one date, in each master's own timezone.
    path(
        "me/day/",
        TenantDayView.as_view(),
        name="tenants-day",
    ),
]
