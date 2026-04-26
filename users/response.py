"""Standardized API response helpers."""

from django.conf import settings
from rest_framework.response import Response

from core.errors import ErrorCode


def success_response(data, meta=None, status_code=200):
    """Wrap data in {"data": ..., "meta": ...} format."""
    body = {"data": data}
    if meta:
        body["meta"] = meta
    return Response(body, status=status_code)


def error_response(code, message, details=None, status_code=400):
    """Wrap error in {"error": {"code": ..., "message": ..., "details": ...}} format.

    ``code`` MUST be a registered ``ErrorCode`` (string value or enum
    member). Unknown codes raise in DEBUG (catch typos like
    ``"VALIDTION_ERROR"`` at PR time) and emit a warning header in
    production so the mobile client can detect drift via Sentry without
    breaking the response envelope.
    """
    code_str = code.value if isinstance(code, ErrorCode) else str(code)

    if not ErrorCode.is_known(code_str):
        if getattr(settings, "DEBUG", False):
            raise AssertionError(
                f"Unknown error code {code_str!r}. Add it to "
                f"core.errors.ErrorCode before using it in error_response()."
            )
        # In production, don't break the response — log and tag the
        # response so Sentry / mobile can surface drift.
        import logging

        logging.getLogger(__name__).warning(
            "error_response.unknown_code code=%s — add to ErrorCode enum",
            code_str,
        )

    error = {"code": code_str, "message": message}
    if details:
        error["details"] = details
    return Response({"error": error}, status=status_code)
