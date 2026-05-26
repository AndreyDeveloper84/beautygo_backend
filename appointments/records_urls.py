"""URL conf for /api/v1/internal/me/bookings/* — task #97.

Customer-records endpoints called by bot-platform via Bearer +
X-External-User-ID. Mounted at ``/api/v1/internal/me/bookings/``
to keep the bot-auth surface clearly separated from JWT user
endpoints (``/api/v1/users/me/*``).
"""
from django.urls import path

from .records_api import (
    MeBookingDetailView,
    MeBookingRepeatIntentView,
    MeBookingsListView,
)


urlpatterns = [
    path(
        "",
        MeBookingsListView.as_view(),
        name="me-bookings-list",
    ),
    path(
        "<uuid:booking_id>/",
        MeBookingDetailView.as_view(),
        name="me-bookings-detail",
    ),
    path(
        "<uuid:booking_id>/repeat-intent/",
        MeBookingRepeatIntentView.as_view(),
        name="me-bookings-repeat-intent",
    ),
]
