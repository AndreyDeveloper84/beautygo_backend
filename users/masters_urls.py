"""URL conf for /api/v1/masters/* — task #92.

Currently only the internal admin batch lookup. Reserved /api/v1/masters/
for future client-facing endpoints aligned with the bot-platform 'master'
terminology (vs Ayla's internal SpecialistProfile naming).
"""
from django.urls import path

from .masters_internal_api import MastersByYclientsStaffIdsView


urlpatterns = [
    path(
        "internal/by-yclients-staff-ids/",
        MastersByYclientsStaffIdsView.as_view(),
        name="masters-internal-by-yclients-staff-ids",
    ),
]
