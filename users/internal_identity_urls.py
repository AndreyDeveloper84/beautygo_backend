"""URL conf for /api/v1/internal/me/identity/ — DRF-1043.

Co-located with the rest of the ``internal/me/`` bot-auth surface
(``me/bookings/`` from #97, ``me/catalog/recommendations/`` from #99) and
mounted the same way: a single-route module included under its own
prefix in the root urlconf.
"""
from django.urls import path

from .internal_identity_api import InternalMeIdentityView


urlpatterns = [
    path(
        "",
        InternalMeIdentityView.as_view(),
        name="internal-me-identity",
    ),
]
