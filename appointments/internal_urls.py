"""URLs for the internal Bearer booking writes (#1016 S2).

Mounted at /api/v1/internal/appointments/ in djangoProject/urls.py.
"""
from django.urls import path

from payments.views import InternalPaymentCreateView

from .internal_api import (
    InternalAppointmentReadView,
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
    # DRF-1233 — the canonical `version`, without which the salon console
    # cannot offer a reschedule at all. Read-only, four fields. Declared
    # last: the converter matches a single segment, so it cannot swallow
    # the `/cancel/` and `/reschedule/` routes above, but keeping the
    # bare-id pattern below them says so at a glance.
    path(
        '<uuid:booking_id>/',
        InternalAppointmentReadView.as_view(),
        name='internal-appointment-read',
    ),
]
