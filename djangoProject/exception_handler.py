"""Central DRF exception handler.

Turns domain-layer exceptions — raised from application services, domain
models, or serializers — into the unified ``{"error": {"code", "message",
"details"}}`` envelope, with the right HTTP status.

Without this handler every view has to repeat the same try/except dance
around ``AuthError``, ``BookingDomainError``, ``SocialAuthError``, and
``serializer.is_valid()``. That's where 80+ copies of the same block came
from and why a single contract change (``VALIDATION_ERROR`` → new schema)
touched 23 files. With this handler, the contract lives in one place.

Wired via ``REST_FRAMEWORK['EXCEPTION_HANDLER']`` in settings/base.py.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler

from core.errors import DomainException, ErrorCode

logger = logging.getLogger(__name__)


def _envelope(
    code: str,
    message: str,
    *,
    details: Optional[Any] = None,
    status_code: int = 400,
) -> Response:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return Response(body, status=status_code)


def api_exception_handler(exc, context):
    """Central DRF exception handler — see module docstring.

    Resolution order (first match wins):

    1. ``core.errors.DomainException`` — the future-going base: code +
       status + details come straight from the exception instance.
    2. Legacy domain bases (``AuthError``, ``SocialAuthError``,
       ``BookingDomainError`` subclasses) still in use across the codebase
       — kept here so the migration to DomainException can happen
       incrementally without a contract break.
    3. DRF built-ins (Validation / NotAuthenticated / PermissionDenied /
       NotFound / Throttled / MethodNotAllowed) — translated into our
       envelope.
    4. Fallback: let DRF's default handler produce a 500 and log the
       unhandled exception with full context.
    """
    # 1. New unified domain base.
    if isinstance(exc, DomainException):
        return _envelope(
            code=exc.code.value,
            message=exc.message,
            details=exc.details,
            status_code=exc.status_code,
        )

    # 2. Legacy domain bases (imported lazily to avoid circular imports
    #    during Django setup — settings can import this module before
    #    users.services is ready).
    legacy_response = _handle_legacy_domain(exc)
    if legacy_response is not None:
        return legacy_response

    # 3. DRF built-ins.
    if isinstance(exc, drf_exceptions.ValidationError):
        return _envelope(
            ErrorCode.VALIDATION_ERROR.value,
            "Invalid input",
            details=exc.detail,
            status_code=400,
        )
    if isinstance(exc, drf_exceptions.NotAuthenticated):
        return _envelope(
            ErrorCode.UNAUTHENTICATED.value,
            str(exc.detail) if hasattr(exc, "detail") else str(exc),
            status_code=401,
        )
    if isinstance(exc, drf_exceptions.AuthenticationFailed):
        return _envelope(
            ErrorCode.AUTHENTICATION_FAILED.value,
            str(exc.detail) if hasattr(exc, "detail") else str(exc),
            status_code=401,
        )
    if isinstance(exc, drf_exceptions.PermissionDenied):
        return _envelope(
            ErrorCode.PERMISSION_DENIED.value,
            str(exc.detail) if hasattr(exc, "detail") else str(exc),
            status_code=403,
        )
    if isinstance(exc, drf_exceptions.NotFound):
        return _envelope(
            ErrorCode.NOT_FOUND.value,
            str(exc.detail) if hasattr(exc, "detail") else str(exc),
            status_code=404,
        )
    if isinstance(exc, drf_exceptions.Throttled):
        return _envelope(
            ErrorCode.THROTTLED.value,
            str(exc.detail) if hasattr(exc, "detail") else str(exc),
            details={"wait_seconds": exc.wait} if exc.wait else None,
            status_code=429,
        )

    # 4. Fallback — DRF default renders DRF APIException subclasses and
    #    lets non-DRF exceptions bubble to a 500 (after logging).
    return drf_default_handler(exc, context)


def _handle_legacy_domain(exc: Exception) -> Optional[Response]:
    """Catch the existing ``AuthError`` / ``BookingDomainError`` /
    ``SocialAuthError`` hierarchies so views stop having to translate them
    by hand. Everything here is intentionally lazy-imported to keep this
    module import-safe before Django's app registry finishes loading.
    """
    try:
        from users.services import AuthError
    except ImportError:  # pragma: no cover — app misconfigured
        AuthError = ()  # type: ignore[assignment]

    try:
        from users.social_auth import SocialAuthError
    except ImportError:  # pragma: no cover
        SocialAuthError = ()  # type: ignore[assignment]

    if AuthError and isinstance(exc, AuthError):
        return _envelope(
            code=getattr(exc, "code", ErrorCode.AUTH_ERROR.value),
            message=getattr(exc, "message", None) or str(exc),
            status_code=getattr(exc, "status_code", 400),
        )

    if SocialAuthError and isinstance(exc, SocialAuthError):
        return _envelope(
            code=getattr(exc, "code", ErrorCode.SOCIAL_AUTH_ERROR.value),
            message=getattr(exc, "message", None) or str(exc),
            status_code=getattr(exc, "status_code", 400),
        )

    return _handle_booking_domain(exc)


def _handle_booking_domain(exc: Exception) -> Optional[Response]:
    try:
        from appointments.domain.exceptions import (
            BookingDomainError,
            BookingWindowError,
            CancellationNotAllowedError,
            InvalidStateTransitionError,
            RescheduleNotAllowedError,
            ServiceNotActiveError,
            SlotNotAvailableError,
            SpecialistNotActiveError,
        )
    except ImportError:  # pragma: no cover
        return None

    if isinstance(exc, SlotNotAvailableError):
        return _envelope(
            ErrorCode.SLOT_NOT_AVAILABLE.value, str(exc), status_code=409,
        )
    if isinstance(exc, InvalidStateTransitionError):
        return _envelope(
            ErrorCode.INVALID_STATE_TRANSITION.value,
            str(exc),
            details={
                "current": getattr(exc, "current", None),
                "target": getattr(exc, "target", None),
            },
            status_code=422,
        )
    if isinstance(exc, BookingWindowError):
        return _envelope(
            ErrorCode.BOOKING_WINDOW_INVALID.value, str(exc), status_code=400,
        )
    if isinstance(exc, RescheduleNotAllowedError):
        return _envelope(
            ErrorCode.RESCHEDULE_NOT_ALLOWED.value, str(exc), status_code=422,
        )
    if isinstance(exc, CancellationNotAllowedError):
        return _envelope(
            ErrorCode.CANCELLATION_NOT_ALLOWED.value, str(exc), status_code=422,
        )
    if isinstance(exc, SpecialistNotActiveError):
        return _envelope(
            ErrorCode.SPECIALIST_NOT_ACTIVE.value, str(exc), status_code=422,
        )
    if isinstance(exc, ServiceNotActiveError):
        return _envelope(
            ErrorCode.SERVICE_NOT_ACTIVE.value, str(exc), status_code=422,
        )
    if isinstance(exc, BookingDomainError):
        return _envelope(
            ErrorCode.BOOKING_ERROR.value, str(exc), status_code=400,
        )

    return None
