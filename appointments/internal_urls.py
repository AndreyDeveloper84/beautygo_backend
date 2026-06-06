"""URLs for the internal Bearer booking writes (#1016 S2).

Mounted at /api/v1/internal/appointments/ in djangoProject/urls.py.
"""
from django.urls import path

from .internal_api import (
    InternalBookingCancelView,
    InternalBookingCreateView,
    InternalBookingRescheduleView,
)

urlpatterns = [
    path('', InternalBookingCreateView.as_view(), name='internal-booking-create'),
    path(
        '<uuid:booking_id>/cancel/',
        InternalBookingCancelView.as_view(),
        name='internal-booking-cancel',
    ),
    path(
        '<uuid:booking_id>/reschedule/',
        InternalBookingRescheduleView.as_view(),
        name='internal-booking-reschedule',
    ),
]
