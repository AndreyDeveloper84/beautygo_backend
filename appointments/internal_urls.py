"""URLs for the internal Bearer booking writes (#1016 S2).

Mounted at /api/v1/internal/appointments/ in djangoProject/urls.py.
"""
from django.urls import path

from payments.views import InternalPaymentCreateView

from .internal_api import (
    InternalBookingCancelView,
    InternalBookingCreateView,
    InternalBookingRescheduleView,
)

urlpatterns = [
    path('', InternalBookingCreateView.as_view(), name='internal-booking-create'),
    # C7.1 — internal payment create (payments app owns the view).
    path(
        '<uuid:appointment_id>/payment/',
        InternalPaymentCreateView.as_view(),
        name='internal-appointment-payment-create',
    ),
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
