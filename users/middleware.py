"""X-App-Type, JWT context, X-Request-ID, and X-Tenant middleware."""

import logging
import uuid

from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from core.log_filters import (
    clear_request_id,
    get_request_id,           # re-export for callers that already imported here
    set_request_id,
)

logger = logging.getLogger(__name__)


__all__ = [
    "AppTypeMiddleware",
    "JWTContextMiddleware",
    "RequestIDMiddleware",
    "TenantContextMiddleware",
    "get_request_id",
]


class RequestIDMiddleware:
    """Stamp every request with an X-Request-ID and echo it in the response.

    If the client sent ``X-Request-ID`` (incoming gateway, mobile app, retry
    helper) we honour it so the same value links external traces to ours.
    Otherwise we generate a UUID. The value lives on the request object,
    in the thread-local read by ``core.log_filters.RequestIDFilter``, and
    in the response header.

    Order: this middleware sits first in MIDDLEWARE so every other layer —
    auth, app-type guard, view, renderer, exception handler — sees the
    same id and emits log lines under it.
    """

    HEADER = "X-Request-ID"
    META_KEY = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.META_KEY, "").strip()
        request_id = incoming or uuid.uuid4().hex
        request.request_id = request_id
        set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            # Drop the value so a thread reused for the next request without
            # going through this middleware (test client edge cases) doesn't
            # see a stale id.
            clear_request_id()
        response[self.HEADER] = request_id
        return response


VALID_APP_TYPES = ("client", "pro")

EXCLUDED_PATH_PREFIXES = (
    "/admin",        # Django Admin (with and without trailing slash)
    "/api/schema/",
    "/api/docs/",
    "/api/redoc/",
    "/api/v1/health/",
    "/static/",
    "/media/",
    # Service-to-service endpoints (DRF-246/247) — auth via X-Service-Token,
    # not X-App-Type. The MAX bot is neither client nor pro.
    "/api/v1/nutrition/internal/",
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


class TenantContextMiddleware:
    """Resolve X-Tenant header → request.tenant (DRF-242.4).

    Reads the ``X-Tenant`` request header (slug, e.g. ``formula``), looks
    up the matching ``tenants.Tenant`` (active rows only via the default
    manager), and attaches the instance to ``request.tenant``. Missing
    or unknown header → ``request.tenant = None``; this is intentionally
    permissive for the rollout phase. DRF-242.5 will introduce the
    ``MULTI_TENANT_STRICT`` setting that flips missing-header to 400.

    Excluded prefixes (no header expected):
    - /admin/                — Django admin uses session auth, not tenant scope.
    - /api/schema/, /docs/, /redoc/  — OpenAPI surfaces.
    - /api/v1/health/        — load balancer probes.
    - /api/v1/nutrition/internal/  — service-to-service path; bot resolves
      its own external_user_id, not via header.
    - /static/, /media/      — file serving.

    Why a separate request attribute instead of overriding request.user.tenant:
    - The header explicitly states *which* tenant the caller is acting in,
      not which tenant the user belongs to. For most users the two match,
      but the ``IsTenantMember`` permission needs both halves of the
      comparison to detect mismatch (header says X, user belongs to Y →
      403).
    - Anonymous / pre-login requests still need a tenant context for
      tenant-scoped catalogue endpoints (e.g. ``/specialists/``).

    Header is case-insensitive at the WSGI layer; Django normalises it to
    ``HTTP_X_TENANT``. Empty string is treated as missing.
    """

    EXCLUDED_PATH_PREFIXES = (
        "/admin",
        "/api/schema/",
        "/api/docs/",
        "/api/redoc/",
        "/api/v1/health/",
        "/api/v1/nutrition/internal/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    # Paths that REQUIRE a tenant header in strict mode (DRF-242.5).
    # Subset of /api/v1/* — auth and anonymous endpoints stay open so
    # mobile clients can register / verify-otp without a tenant context
    # (the registration flow is what assigns the tenant in the first place).
    STRICT_REQUIRED_PREFIXES = (
        "/api/v1/",
    )
    STRICT_OPT_OUT_PREFIXES = (
        # Auth handshake — pre-tenant. Includes /auth/login/, /verify-otp/,
        # /register/, /anonymous/, /token/refresh/, /logout/, /social/, etc.
        "/api/v1/auth/",
    )

    def __call__(self, request):
        from django.conf import settings

        request.tenant = None

        path = request.path
        if any(path.startswith(p) for p in self.EXCLUDED_PATH_PREFIXES):
            return self.get_response(request)

        slug = (request.META.get("HTTP_X_TENANT") or "").strip()

        # Local import to avoid app-loading order issues — middleware
        # is imported during settings bootstrap, before INSTALLED_APPS
        # is fully populated.
        from tenants.models import Tenant

        if slug:
            try:
                request.tenant = Tenant.objects.get(slug=slug)
            except Tenant.DoesNotExist:
                # Unknown / inactive slug → leave request.tenant=None.
                # In strict mode this falls through to the 400 below.
                logger.info(
                    "tenant.middleware.unknown_slug slug=%s path=%s",
                    slug, path,
                )
        else:
            # DRF-242.8: fall back to the JWT ``tenant_id`` claim when
            # the mobile client forgets the X-Tenant header. The claim
            # is re-resolved on every refresh, so it can't drift more
            # than ACCESS_TOKEN_LIFETIME from the live row. Header takes
            # precedence: explicit caller intent beats inferred state.
            tenant_id = self._jwt_tenant_id(request)
            if tenant_id:
                try:
                    request.tenant = Tenant.objects.get(pk=tenant_id)
                except (Tenant.DoesNotExist, ValueError):
                    logger.info(
                        "tenant.middleware.unknown_jwt_tenant id=%s path=%s",
                        tenant_id, path,
                    )

        # Strict mode (DRF-242.5): /api/v1/* requires a valid X-Tenant
        # except for auth handshake. Returns 400 instead of letting the
        # permission layer 403 — distinguishes "you forgot the header"
        # from "you don't belong to this tenant".
        if (
            getattr(settings, "MULTI_TENANT_STRICT", False)
            and request.tenant is None
            and any(path.startswith(p) for p in self.STRICT_REQUIRED_PREFIXES)
            and not any(path.startswith(p) for p in self.STRICT_OPT_OUT_PREFIXES)
        ):
            return JsonResponse(
                {"error": {
                    "code": "TENANT_REQUIRED",
                    "message": (
                        "Заголовок X-Tenant обязателен. "
                        "Если вы не знаете свой tenant, обратитесь в поддержку."
                    ),
                }},
                status=400,
            )

        return self.get_response(request)

    @staticmethod
    def _jwt_tenant_id(request) -> str | None:
        """Extract ``tenant_id`` claim from the validated JWT, if any.

        Reads the same Authorization header SimpleJWT uses, decodes
        without DB lookup (the user check is the auth backend's job),
        and returns the raw claim value. Returns None for any decode
        error or missing claim — middleware should silently fall back
        to permissive mode in that case, mirroring the unknown-slug
        branch above.
        """
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth.startswith("Bearer "):
            return None
        raw_token = auth[len("Bearer "):].strip()
        if not raw_token:
            return None
        # Local import — same app-loading-order rationale as Tenant above.
        from rest_framework_simplejwt.exceptions import (
            InvalidToken,
            TokenError,
        )
        from rest_framework_simplejwt.tokens import AccessToken

        try:
            token = AccessToken(raw_token)
        except (InvalidToken, TokenError):
            return None
        return token.get("tenant_id")
