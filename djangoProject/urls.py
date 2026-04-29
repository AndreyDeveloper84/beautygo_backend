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


urlpatterns = [
    path('admin/', admin.site.urls),

    # API v1
    path('api/v1/auth/', include('users.urls')),
    path('api/v1/users/', include('users.users_urls')),
    path('api/v1/services/', include('services.urls')),
    path('api/v1/service-templates/', include('services.templates_urls')),
    path('api/v1/categories/', include('services.categories_urls')),
    path('api/v1/specialists/', include('users.specialists_urls')),
    path('api/v1/appointments/', include('appointments.urls')),
    path('api/v1/reviews/', include('reviews.urls')),
    path('api/v1/payments/', include('payments.urls')),
    path('api/v1/search/', include('search.urls')),
    path('api/v1/devices/', include('users.devices_urls')),
    path('api/v1/ai/', include('ai.urls', namespace='ai')),
    path('api/v1/nutrition/', include('nutrition.urls', namespace='nutrition')),
    path('api/v1/notifications/', include('notifications.urls', namespace='notifications')),
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
