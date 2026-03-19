"""X-App-Type header middleware."""

import json

from django.conf import settings
from django.http import JsonResponse


VALID_APP_TYPES = ("client", "pro")

EXCLUDED_PATH_PREFIXES = (
    "/admin/",
    "/api/schema/",
    "/api/docs/",
    "/api/redoc/",
    "/api/v1/health/",
    "/static/",
    "/media/",
)


class AppTypeMiddleware:
    """Enforce X-App-Type header on all API requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        if any(path.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
            request.app_type = None
            return self.get_response(request)

        app_type = request.META.get("HTTP_X_APP_TYPE")

        if not app_type:
            return JsonResponse(
                {"error": {"code": "APP_TYPE_MISSING", "message": "X-App-Type header is required"}},
                status=403,
            )

        if app_type not in VALID_APP_TYPES:
            return JsonResponse(
                {"error": {"code": "APP_TYPE_INVALID", "message": f"X-App-Type must be one of: {', '.join(VALID_APP_TYPES)}"}},
                status=403,
            )

        request.app_type = app_type
        return self.get_response(request)
