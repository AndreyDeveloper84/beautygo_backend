"""URL conf for /api/v1/tenants/* — #246 Q1+ admin endpoints.

The salon-admin surface: everything an administrator does to their own
salon rather than to their own profile. Started with access revocation;
DRF-1062 added schedule management for any master of the tenant, which
is what lets a salon stop selling closed hours; DRF-1063 adds the day
journal — the first endpoint where a salon employee can see the work of
masters other than themselves.
"""
from django.urls import path

from users.schedule_admin_api import (
    AdminScheduleExceptionDetailView,
    AdminScheduleExceptionListView,
    AdminScheduleImpactView,
    AdminScheduleView,
    AdminTimeOffDetailView,
    AdminTimeOffListView,
    TenantClosureDetailView,
    TenantClosureListView,
)

from .appointments_api import (
    SalonBookingCancelView,
    SalonBookingCompleteView,
    SalonBookingCreateView,
    SalonBookingRescheduleView,
    SalonCustomerLookupView,
)
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
    # DRF-1063 block D — the three things a front desk does all day.
    # Customer lookup lives here rather than under a customers tab
    # because the Master Schedule UX Contract puts it inside the booking
    # flow.
    path(
        "me/customers/",
        SalonCustomerLookupView.as_view(),
        name="tenants-customer-lookup",
    ),
    path(
        "me/appointments/",
        SalonBookingCreateView.as_view(),
        name="tenants-booking-create",
    ),
    path(
        "me/appointments/<uuid:appointment_id>/reschedule/",
        SalonBookingRescheduleView.as_view(),
        name="tenants-booking-reschedule",
    ),
    path(
        "me/appointments/<uuid:appointment_id>/cancel/",
        SalonBookingCancelView.as_view(),
        name="tenants-booking-cancel",
    ),
    # DRF-1234 — closing the visit. The mobile endpoint already accepts a
    # salon administrator (DRF-1064); this is the same steps on a surface
    # the bot can actually reach. `no_show` is deliberately not here —
    # see the view docstring.
    path(
        "me/appointments/<uuid:appointment_id>/complete/",
        SalonBookingCompleteView.as_view(),
        name="tenants-booking-complete",
    ),
    # DRF-1062 — schedule of any master in this tenant. The pro-app
    # routes under /specialists/me/ stay untouched; these are the same
    # views with the master resolved from the URL instead of the session.
    path(
        "me/masters/<uuid:specialist_id>/schedule/",
        AdminScheduleView.as_view(),
        name="tenants-master-schedule",
    ),
    # Preview before blocking: which live bookings this absence displaces.
    path(
        "me/masters/<uuid:specialist_id>/schedule/impact/",
        AdminScheduleImpactView.as_view(),
        name="tenants-master-schedule-impact",
    ),
    path(
        "me/masters/<uuid:specialist_id>/time-off/",
        AdminTimeOffListView.as_view(),
        name="tenants-master-time-off",
    ),
    path(
        "me/masters/<uuid:specialist_id>/time-off/<uuid:pk>/",
        AdminTimeOffDetailView.as_view(),
        name="tenants-master-time-off-detail",
    ),
    path(
        "me/masters/<uuid:specialist_id>/schedule-exceptions/",
        AdminScheduleExceptionListView.as_view(),
        name="tenants-master-schedule-exceptions",
    ),
    path(
        "me/masters/<uuid:specialist_id>/schedule-exceptions/<slug:date>/",
        AdminScheduleExceptionDetailView.as_view(),
        name="tenants-master-schedule-exception-detail",
    ),
    path(
        "me/closures/",
        TenantClosureListView.as_view(),
        name="tenants-closures",
    ),
    path(
        "me/closures/<uuid:pk>/",
        TenantClosureDetailView.as_view(),
        name="tenants-closure-detail",
    ),
]
