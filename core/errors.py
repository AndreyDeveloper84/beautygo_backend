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
    """Single source of truth for ``error.code`` strings.

    Cross-referenced with Notion API Spec v2.0 §Validation Rules — the
    spec catalogue is the public contract that the mobile client codegens
    against. Codes here that are NOT in the spec are project-internal
    (auth-flow edges, AI Chat extensions) and should be propagated to
    Notion when the spec is next refreshed; see CLAUDE.md.
    """

    # --- Validation / input (spec §Общие) ---
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FORMAT = "INVALID_FORMAT"
    MISSING_PARAM = "MISSING_PARAM"
    INVALID_PARAM = "INVALID_PARAM"
    # Internal (bot) reschedule/cancel — X-Idempotency-Key is mandatory,
    # not just recommended (see appointments.internal_api). Same
    # missing-required-header shape as TENANT_REQUIRED (users.middleware).
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"

    # --- Authentication / authorization (spec §Аутентификация) ---
    AUTH_ERROR = "AUTH_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_MISSING = "TOKEN_MISSING"
    INVALID_TOKEN = "INVALID_TOKEN"  # legacy alias of TOKEN_INVALID
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    REFRESH_TOKEN_INVALID = "REFRESH_TOKEN_INVALID"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    PHONE_NOT_VERIFIED = "PHONE_NOT_VERIFIED"
    ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
    PERMISSION_DENIED = "PERMISSION_DENIED"  # project alias of ROLE_NOT_ALLOWED
    FORBIDDEN = "FORBIDDEN"  # legacy — prefer PERMISSION_DENIED
    FORBIDDEN_FOR_ANONYMOUS = "FORBIDDEN_FOR_ANONYMOUS"
    WRONG_APP_TYPE = "WRONG_APP_TYPE"
    NOT_OWNER = "NOT_OWNER"
    NOT_PARTICIPANT = "NOT_PARTICIPANT"
    NOT_REVIEW_OWNER = "NOT_REVIEW_OWNER"

    # --- OTP / phone auth (spec §SMS-коды + §Регистрация) ---
    PHONE_ALREADY_REGISTERED = "PHONE_ALREADY_REGISTERED"
    PHONE_ALREADY_BOUND = "PHONE_ALREADY_BOUND"
    PHONE_EXISTS = "PHONE_EXISTS"
    EMAIL_EXISTS = "EMAIL_EXISTS"
    INVALID_PHONE_FORMAT = "INVALID_PHONE_FORMAT"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    INVALID_OTP = "INVALID_OTP"
    INVALID_CODE = "INVALID_CODE"
    OTP_EXPIRED = "OTP_EXPIRED"
    CODE_EXPIRED = "CODE_EXPIRED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"
    SMS_RATE_LIMITED = "SMS_RATE_LIMITED"

    # --- Social auth ---
    SOCIAL_AUTH_ERROR = "SOCIAL_AUTH_ERROR"
    INVALID_PROVIDER = "INVALID_PROVIDER"
    SOCIAL_TOKEN_INVALID = "SOCIAL_TOKEN_INVALID"

    # --- Profile ---
    PROFILE_EXISTS = "PROFILE_EXISTS"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"

    # --- Specialists & services (spec §Мастера и услуги) ---
    SPECIALIST_NOT_FOUND = "SPECIALIST_NOT_FOUND"
    SERVICE_NOT_FOUND = "SERVICE_NOT_FOUND"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    SERVICE_INACTIVE = "SERVICE_INACTIVE"
    SPECIALIST_UNAVAILABLE = "SPECIALIST_UNAVAILABLE"
    HAS_APPOINTMENTS = "HAS_APPOINTMENTS"
    HAS_ACTIVE_APPOINTMENTS = "HAS_ACTIVE_APPOINTMENTS"  # alias of HAS_APPOINTMENTS

    # --- Schedule & slots (spec §Расписание и слоты) ---
    DATE_IN_PAST = "DATE_IN_PAST"
    DATE_TOO_FAR = "DATE_TOO_FAR"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    INVALID_BREAK = "INVALID_BREAK"
    # DRF-1062 — salon-admin schedule management.
    CLOSURE_EXISTS = "CLOSURE_EXISTS"
    # The bookings displaced by an absence changed between the preview the
    # administrator decided on and the confirmation — re-ask rather than
    # apply decisions to a set that no longer exists.
    IMPACT_CHANGED = "IMPACT_CHANGED"
    UNSUPPORTED_RESOLUTION = "UNSUPPORTED_RESOLUTION"
    WORKING_DAY_OFF = "WORKING_DAY_OFF"

    # --- Booking domain (spec §Записи) ---
    SLOT_NOT_AVAILABLE = "SLOT_NOT_AVAILABLE"
    INVALID_DATETIME = "INVALID_DATETIME"
    DATETIME_TOO_SOON = "DATETIME_TOO_SOON"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"  # spec spelling
    INVALID_STATUS = "INVALID_STATUS"
    APPOINTMENT_NOT_FOUND = "APPOINTMENT_NOT_FOUND"
    DUPLICATE_APPOINTMENT = "DUPLICATE_APPOINTMENT"
    CANCELLATION_TOO_LATE = "CANCELLATION_TOO_LATE"
    RESCHEDULE_TOO_LATE = "RESCHEDULE_TOO_LATE"
    BOOKING_WINDOW_INVALID = "BOOKING_WINDOW_INVALID"
    RESCHEDULE_NOT_ALLOWED = "RESCHEDULE_NOT_ALLOWED"
    # Wave 1 concurrency contract — see appointments.domain.exceptions.
    STALE_VERSION = "STALE_VERSION"
    APPOINTMENT_TERMINAL = "APPOINTMENT_TERMINAL"
    EXPECTED_VERSION_REQUIRED = "EXPECTED_VERSION_REQUIRED"
    CANCELLATION_NOT_ALLOWED = "CANCELLATION_NOT_ALLOWED"
    SPECIALIST_NOT_ACTIVE = "SPECIALIST_NOT_ACTIVE"
    SERVICE_NOT_ACTIVE = "SERVICE_NOT_ACTIVE"
    BOOKING_ERROR = "BOOKING_ERROR"

    # --- Reviews (spec §Отзывы) ---
    REVIEW_EXISTS = "REVIEW_EXISTS"
    REVIEW_NOT_FOUND = "REVIEW_NOT_FOUND"
    APPOINTMENT_NOT_COMPLETED = "APPOINTMENT_NOT_COMPLETED"

    # --- Payments (spec §Платежи) ---
    PAYMENT_ERROR = "PAYMENT_ERROR"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    PAYMENT_ALREADY_COMPLETED = "PAYMENT_ALREADY_COMPLETED"
    APPOINTMENT_ALREADY_PAID = "APPOINTMENT_ALREADY_PAID"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    REFUND_AMOUNT_EXCEEDS_PAID = "REFUND_AMOUNT_EXCEEDS_PAID"
    REFUND_NOT_ALLOWED = "REFUND_NOT_ALLOWED"
    PAYMENT_PROVIDER_ERROR = "PAYMENT_PROVIDER_ERROR"
    # W2 billing — pay-debt endpoint: 409 when the subscription has no
    # outstanding debt (no failed/open invoice).
    NO_DEBT = "NO_DEBT"

    # --- AI Chat (spec §AI) ---
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    AI_RATE_LIMITED = "AI_RATE_LIMITED"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    INVALID_ACTION = "INVALID_ACTION"
    INVALID_ACTION_TYPE = "INVALID_ACTION_TYPE"  # project alias of INVALID_ACTION
    ACTION_EXPIRED = "ACTION_EXPIRED"

    # --- Food Scanner (spec §Food Scanner) ---
    FOOD_NOT_RECOGNIZED = "FOOD_NOT_RECOGNIZED"
    FOOD_API_UNAVAILABLE = "FOOD_API_UNAVAILABLE"
    SCAN_NOT_FOUND = "SCAN_NOT_FOUND"

    # --- File upload (spec §Файл изображения) ---
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_FILE_TYPE = "INVALID_FILE_TYPE"

    # --- Analytics (project — not in spec, mobile-side codegen target) ---
    UNKNOWN_EVENT_NAME = "UNKNOWN_EVENT_NAME"

    # --- Resource ---
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"

    # --- Goal anketa (DRF-1451; project — not in spec) ---
    # Ответ на шаг анкеты разошёлся с тем шагом, который ждёт сервер:
    # документ на экране протух. Клиенту сюда — перечитать документ,
    # а не повторять тот же запрос.
    ANKETA_STEP_MISMATCH = "ANKETA_STEP_MISMATCH"

    # --- Rate limiting ---
    THROTTLED = "THROTTLED"
    RATE_LIMITED = "RATE_LIMITED"

    # --- Catch-all server ---
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    @classmethod
    def is_known(cls, code: str) -> bool:
        """O(1) membership check used by ``users.response.error_response``."""
        return code in cls._value2member_map_


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
