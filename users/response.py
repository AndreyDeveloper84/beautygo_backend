"""Standardized API response helpers."""

from rest_framework.response import Response


def success_response(data, meta=None, status_code=200):
    """Wrap data in {"data": ..., "meta": ...} format."""
    body = {"data": data}
    if meta:
        body["meta"] = meta
    return Response(body, status=status_code)


def error_response(code, message, details=None, status_code=400):
    """Wrap error in {"error": {"code": ..., "message": ..., "details": ...}} format."""
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return Response({"error": error}, status=status_code)
