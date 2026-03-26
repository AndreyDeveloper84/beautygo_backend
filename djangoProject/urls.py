from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)


def health_check(request):
    """Health check endpoint."""
    return JsonResponse({"status": "ok", "version": "1.0.0"})


urlpatterns = [
    path('admin/', admin.site.urls),

    # API v1
    path('api/v1/auth/', include('users.urls')),
    path('api/v1/services/', include('services.urls')),
    path('api/v1/categories/', include('services.categories_urls')),
    path('api/v1/specialists/', include('users.specialists_urls')),
    path('api/v1/appointments/', include('appointments.urls')),

    # Health
    path('api/v1/health/', health_check, name='health-check'),

    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
