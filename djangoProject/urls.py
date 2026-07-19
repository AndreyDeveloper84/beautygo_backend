from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

from .health import liveness, readiness
from payments.views import InternalPayoutPreviewView


urlpatterns = [
    path('admin/', admin.site.urls),

    # API v1
    path('api/v1/auth/', include('users.urls')),
    path('api/v1/users/', include('users.users_urls')),
    path('api/v1/services/', include('services.urls')),
    path('api/v1/service-templates/', include('services.templates_urls')),
    path('api/v1/categories/', include('services.categories_urls')),
    path('api/v1/specialists/', include('users.specialists_urls')),
    path('api/v1/masters/', include('users.masters_urls')),
    path('api/v1/tenants/', include('tenants.urls')),
    path(
        'api/v1/internal/me/bookings/',
        include('appointments.records_urls'),
    ),
    # P1-3 codex audit — internal user-profile fetch for the bot's
    # user.profile.updated consumer (#446). PII §7 closed subset
    # (display_name + avatar_url only); Bearer-auth only.
    path(
        'api/v1/internal/users/',
        include('users.internal_users_urls'),
    ),
    path(
        'api/v1/internal/me/catalog/recommendations/',
        include('users.catalog_recommendations_urls'),
    ),
    # #1016 S2 — internal Bearer REST surface the Ayla bot reads/writes
    # (slots + catalog mirror + booking create/cancel/reschedule).
    # Contract co-owned with S1: ai-bot-platform/docs/architecture/.
    # C3 payout preview — explicit route BEFORE the specialists include
    # so the resolver doesn't feed "payout-preview" into the include's
    # patterns (owner: payments/, PILOT_CONTRACTS §4).
    path(
        'api/v1/internal/specialists/<uuid:specialist_id>/payout-preview/',
        InternalPayoutPreviewView.as_view(),
        name='internal-payout-preview',
    ),
    path(
        'api/v1/internal/specialists/',
        include('users.internal_catalog_urls'),
    ),
    path(
        'api/v1/internal/services/',
        include('services.internal_urls'),
    ),
    # S3A canonical catalog mirror (#1044 / #200) — new SalonService /
    # SpecialistService layer the bot (S3B) reads.
    path(
        'api/v1/internal/catalog/',
        include('services.internal_catalog_urls'),
    ),
    path(
        'api/v1/internal/appointments/',
        include('appointments.internal_urls'),
    ),
    path('api/v1/appointments/', include('appointments.urls')),
    path('api/v1/reviews/', include('reviews.urls')),
    path('api/v1/payments/', include('payments.urls')),
    path('api/v1/search/', include('search.urls')),
    path('api/v1/devices/', include('users.devices_urls')),
    path('api/v1/favorites/', include('users.favorites_urls', namespace='favorites')),
    path('api/v1/ai/', include('ai.urls', namespace='ai')),
    path('api/v1/nutrition/', include('nutrition.urls', namespace='nutrition')),
    path('api/v1/notifications/', include('notifications.urls', namespace='notifications')),
    path('api/v1/analytics/', include('analytics.urls', namespace='analytics')),
    path(
        'api/v1/home/',
        __import__('users.home_api', fromlist=['HomeView']).HomeView.as_view(),
        name='home',
    ),

    # Health: liveness for loadbalancer, readiness for deploy + on-call.
    path('api/v1/health/', liveness, name='health-check'),
    path('api/v1/health/ready/', readiness, name='health-ready'),

    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
