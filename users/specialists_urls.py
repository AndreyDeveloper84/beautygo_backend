from django.urls import path
from rest_framework.routers import DefaultRouter

from reviews.views import SpecialistReviewsView

from .portfolio_api import PortfolioDetailView, PortfolioListCreateView
from .schedule_api import ScheduleView, TimeOffDetailView, TimeOffListView
from .specialists_api import SpecialistViewSet
from .views import MasterMeView

router = DefaultRouter()
router.register(r'', SpecialistViewSet, basename='specialists')

urlpatterns = [
    # Canonical reviews-by-specialist endpoint per spec v2.0. Used to live
    # as a permission-bypass @action on SpecialistViewSet that late-imported
    # reviews.views — moved here to keep the cross-app dependency at the
    # routing layer where it's visible.
    path(
        '<uuid:specialist_id>/reviews/',
        SpecialistReviewsView.as_view(),
        name='specialist-reviews',
    ),

    # Canonical specialist profile (DRF-209). Legacy /api/v1/auth/masters/me/
    # stays live in users/urls.py wrapped with deprecation headers — see
    # LegacyMasterMeView there.
    path('me/', MasterMeView.as_view(), name='specialist-me'),

    # Specialist schedule management (Pro app) — must come before router patterns
    path('me/schedule/', ScheduleView.as_view(), name='specialist-schedule'),
    path('me/time-off/', TimeOffListView.as_view(), name='specialist-time-off'),
    path('me/time-off/<uuid:pk>/', TimeOffDetailView.as_view(), name='specialist-time-off-detail'),

    # Spec-compliant aliases (Notion API spec)
    path('me/working-hours/', ScheduleView.as_view(), name='specialist-working-hours'),
    path('me/schedule/blocks/', TimeOffListView.as_view(), name='specialist-schedule-blocks'),
    path('me/schedule/blocks/<uuid:pk>/', TimeOffDetailView.as_view(), name='specialist-schedule-block-detail'),

    # Portfolio (Pro app, DRF-194). Public read uses the embedded list
    # in GET /specialists/{id}/ — see SpecialistDetailSerializer.
    path('me/portfolio/', PortfolioListCreateView.as_view(), name='specialist-portfolio'),
    path('me/portfolio/<uuid:pk>/', PortfolioDetailView.as_view(), name='specialist-portfolio-detail'),
] + router.urls
