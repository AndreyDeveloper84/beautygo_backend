"""Central error taxonomy.

Everything that gets returned inside an ``{"error": {"code": ..., ...}}``
envelope has its code listed here. Keeping them in one enum means:

- the mobile client can codegen an exhaustive switch without guessing;
- a typo in a string literal (e.g. "VALIDTION_ERROR") fails at import time,
  not in production under a rare code path;
- every rename is a single-file change instead of a 23-file safari.

Add a new code here first, then reference ``ErrorCode.X.value`` from domain
exceptions or view-layer ``error_response()`` calls.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    # --- Validation / input ---
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MISSING_PARAM = "MISSING_PARAM"
    INVALID_PARAM = "INVALID_PARAM"

    # --- Authentication / authorization ---
    AUTH_ERROR = "AUTH_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    WRONG_APP_TYPE = "WRONG_APP_TYPE"
    INVALID_TOKEN = "INVALID_TOKEN"

    # --- OTP / phone auth ---
    PHONE_ALREADY_REGISTERED = "PHONE_ALREADY_REGISTERED"
    PHONE_ALREADY_BOUND = "PHONE_ALREADY_BOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_OTP = "INVALID_OTP"
    OTP_EXPIRED = "OTP_EXPIRED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"

    # --- Social auth ---
    SOCIAL_AUTH_ERROR = "SOCIAL_AUTH_ERROR"
    INVALID_PROVIDER = "INVALID_PROVIDER"
    SOCIAL_TOKEN_INVALID = "SOCIAL_TOKEN_INVALID"

    # --- Booking domain ---
    SLOT_NOT_AVAILABLE = "SLOT_NOT_AVAILABLE"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    BOOKING_WINDOW_INVALID = "BOOKING_WINDOW_INVALID"
    RESCHEDULE_NOT_ALLOWED = "RESCHEDULE_NOT_ALLOWED"
    CANCELLATION_NOT_ALLOWED = "CANCELLATION_NOT_ALLOWED"
    SPECIALIST_NOT_ACTIVE = "SPECIALIST_NOT_ACTIVE"
    SERVICE_NOT_ACTIVE = "SERVICE_NOT_ACTIVE"
    BOOKING_ERROR = "BOOKING_ERROR"

    # --- Reviews ---
    REVIEW_EXISTS = "REVIEW_EXISTS"

    # --- Payments ---
    PAYMENT_ERROR = "PAYMENT_ERROR"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    INVALID_AMOUNT = "INVALID_AMOUNT"

    # --- Resource ---
    NOT_FOUND = "NOT_FOUND"

    # --- Rate limiting ---
    THROTTLED = "THROTTLED"

    # --- Catch-all server ---
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainException(Exception):
    """Base for any business-level error that should be rendered as a
    structured API response by the central exception handler.

    Subclasses set ``code`` (ErrorCode), ``status_code`` (int) and an
    optional default ``message``. The exception handler reads those and
    returns ``{"error": {"code": ..., "message": ..., "details": ...}}``
    with the right HTTP status.

    For dynamic details (field-level validation breakdowns, slot conflict
    metadata, etc.) pass ``details`` at the call site.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = 400
    default_message: str = ""

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.default_message or self.code.value
        self.details = details
        super().__init__(self.message)
