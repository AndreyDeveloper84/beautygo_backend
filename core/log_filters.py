"""Logging-side helpers — kept Django-free so settings.LOGGING can
import them before the app registry is ready.

The actual middleware that PUTS a request id into thread-local lives in
``users.middleware`` (it has Django dependencies). This module reads
that thread-local — nothing more — so it imports cleanly during
``settings`` evaluation.
"""
from __future__ import annotations

import logging
import threading


# Module-level thread-local. ``users.middleware.RequestIDMiddleware`` is
# the writer; everything else only reads.
_state = threading.local()

NO_REQUEST_SENTINEL = "-"


def set_request_id(value: str) -> None:
    _state.request_id = value


def clear_request_id() -> None:
    _state.request_id = NO_REQUEST_SENTINEL


def get_request_id() -> str:
    return getattr(_state, "request_id", NO_REQUEST_SENTINEL)


class RequestIDFilter(logging.Filter):
    """Inject the current request id into every log record.

    Wired into ``LOGGING['filters']``. Without this the
    ``%(request_id)s`` placeholder in formatters would raise KeyError
    on records emitted outside a request (mgmt commands, startup, etc).
    """

    def filter(self, record):
        record.request_id = get_request_id()
        return True
