from django.urls import path
from rest_framework.routers import DefaultRouter

from .schedule_api import ScheduleView, TimeOffDetailView, TimeOffListView
from .specialists_api import SpecialistViewSet

router = DefaultRouter()
router.register(r'', SpecialistViewSet, basename='specialists')

urlpatterns = [
    # Specialist schedule management (Pro app) — must come before router patterns
    path('me/schedule/', ScheduleView.as_view(), name='specialist-schedule'),
    path('me/time-off/', TimeOffListView.as_view(), name='specialist-time-off'),
    path('me/time-off/<uuid:pk>/', TimeOffDetailView.as_view(), name='specialist-time-off-detail'),

    # Spec-compliant aliases (Notion API spec)
    path('me/working-hours/', ScheduleView.as_view(), name='specialist-working-hours'),
    path('me/schedule/blocks/', TimeOffListView.as_view(), name='specialist-schedule-blocks'),
    path('me/schedule/blocks/<uuid:pk>/', TimeOffDetailView.as_view(), name='specialist-schedule-block-detail'),
] + router.urls
