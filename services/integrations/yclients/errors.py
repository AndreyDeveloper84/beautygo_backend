"""YClients integration exceptions."""
from __future__ import annotations


class YClientsError(Exception):
    """Base for all YClients integration failures."""


class YClientsConfigError(YClientsError):
    """Missing / invalid credentials or company id — fail-closed."""


class YClientsAuthError(YClientsError):
    """Authentication rejected (401 / 403 without a business message)."""


class YClientsBusinessError(YClientsError):
    """API returned ``success: false`` with a business-level message.

    This is how YClients surfaces domain conditions such as an expired
    salon licence (``meta.message``), not a transport failure.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class YClientsAPIError(YClientsError):
    """Non-retryable HTTP failure, or a transient one that exhausted retries."""


class YClientsTimeoutError(YClientsError):
    """Timeout / connection error that exhausted retries."""
