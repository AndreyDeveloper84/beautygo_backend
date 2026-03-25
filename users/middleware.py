"""X-App-Type header and JWT context middleware."""

import logging

from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

logger = logging.getLogger(__name__)


VALID_APP_TYPES = ("client", "pro")

EXCLUDED_PATH_PREFIXES = (
    "/admin",        # Django Admin (with and without trailing slash)
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
                {"error": {
                    "code": "APP_TYPE_MISSING",
                    "message": "X-App-Type header is required",
                }},
                status=403,
            )

        if app_type not in VALID_APP_TYPES:
            return JsonResponse(
                {"error": {
                    "code": "APP_TYPE_INVALID",
                    "message": (
                        f"X-App-Type must be one of: "
                        f"{', '.join(VALID_APP_TYPES)}"
                    ),
                }},
                status=403,
            )

        request.app_type = app_type
        return self.get_response(request)


class JWTContextMiddleware:
    """Decode JWT and add user_id + role to request context."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.jwt_auth = JWTAuthentication()

    def __call__(self, request):
        request.user_id = None
        request.user_role = None

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            try:
                validated_token = self.jwt_auth.get_validated_token(
                    auth_header.split(" ", 1)[1]
                )
                user = self.jwt_auth.get_user(validated_token)
                request.user_id = str(user.pk)
                request.user_role = user.role

                # Device ID check: reauth required on device change
                token_device_id = validated_token.get('device_id')
                request_device_id = request.META.get(
                    "HTTP_X_DEVICE_ID",
                )
                if (
                    token_device_id
                    and request_device_id
                    and token_device_id != request_device_id
                ):
                    return JsonResponse(
                        {"error": {
                            "code": "DEVICE_MISMATCH",
                            "message": "Device changed, re-authentication required",
                        }},
                        status=401,
                    )
            except (InvalidToken, TokenError):
                pass
            except Exception:
                logger.exception("Unexpected error in JWTContextMiddleware")

        return self.get_response(request)
