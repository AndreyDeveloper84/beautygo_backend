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


def _normalize_validation_details(detail: Any) -> Any:
    """Canonical ``details`` for a list-serializer failure — the same shape
    on every DRF version.

    DRF ≤ 3.16 renders ``many=True`` errors as a positional list with ``{}``
    for the items that passed; DRF ≥ 3.17 renders a dict keyed by the item
    index as a string, omitting the items that passed. The public contract
    (``docs/PERSONAL_CONTEXT_INTERNAL_API_CONTRACT.md`` — ``details?: {...}``)
    promises an object, so the dict form is canonical and the list form is
    translated into it here, at the single place the envelope is built.

    Recorded on both versions before this existed —
    ``Ayla/docs/DRF_318_GATE_2026-09-12.md`` (DRF-1714): the only production
    ``many=True`` validation in the catalog is
    ``users/internal_personal_context_api.py`` ``_UpdateItemSerializer``.
    Anything that is not a list (a plain dict from a single serializer, a
    list of strings from ``non_field_errors``) is returned untouched.
    """
    if not isinstance(detail, list):
        return detail
    if not all(isinstance(item, dict) for item in detail):
        # ``["message", ...]`` — a field-level list of messages, not a list of
        # per-item error dicts. Leave it alone: it is not the many=True shape.
        return detail
    return {str(index): item for index, item in enumerate(detail) if item}


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
            details=_normalize_validation_details(exc.detail),
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
            HealthScreeningRequiredError,
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
    if isinstance(exc, HealthScreeningRequiredError):
        # Решение владельца (c) от 10.09.2026. Три вещи здесь намеренны.
        #
        # СВОЙ КОД, А НЕ ``BOOKING_ERROR``. Общий код заставил бы каждую
        # поверхность угадывать исход по тексту сообщения, а очередь
        # разметки услуг — строиться на грепе логов. Кодов два, потому
        # что REQUIRED и UNKNOWN — разные положения человека и разная
        # работа оператора: первому нужен скрининг, второму — ответ
        # салона.
        #
        # ``handoff: true`` В ``details``. Это не украшение конверта, а
        # единственный машинный признак, по которому поверхность отличает
        # «показать ошибку» от «позвать человека». Тест поверхности
        # проверяет именно его, а не строку текста.
        #
        # 4xx, А НЕ 2xx, И ЭТО ГЛАВНОЕ. Соблазн отдать 200 с телом «не
        # ошибка» велик, но он ломается на неподнятом потребителе:
        # клиент, который считает 2xx успехом, покажет «вы записаны» на
        # ответе без записи — ровно то, что владелец запретил («не
        # обещать, что запись создана»). При 4xx необновлённая
        # поверхность деградирует в «ошибку»: тон неверный, обещания
        # ложного нет. Из двух видов деградации выбран безопасный.
        # Создание отвечает 201; этот исход не отвечает 2xx никогда.
        return _envelope(
            exc.reason,
            exc.text,
            details={"handoff": exc.leads_to_a_human, "reason": exc.reason},
            status_code=422,
        )
    if isinstance(exc, BookingDomainError):
        return _envelope(
            ErrorCode.BOOKING_ERROR.value, str(exc), status_code=400,
        )

    return None
