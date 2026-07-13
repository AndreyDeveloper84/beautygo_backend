"""Read-only YClients API client for catalog intake (S3C).

Pull-only — never mutates the salon's YClients account. Mirrors the
outbound-HTTP conventions of ``appointments/infrastructure/outbox/
publisher.py``: ``requests``, creds resolved from settings with
fail-closed defaults, timeout, and retry/backoff on transient failures.

Verified contract (design §10, live 2026-07-10 against company 884045):
- auth: ``Authorization: Bearer <partner>, User <user>`` (both in one
  header), ``Accept: application/vnd.yclients.v2+json``
- envelope: ``{success, data, meta}``; ``success:false`` carries a
  business message in ``meta.message`` (e.g. expired-licence 403)
- rate limit: ``X-RateLimit-*`` (200/window); 429 → retry with backoff
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

from services.integrations.yclients.errors import (
    YClientsAPIError,
    YClientsAuthError,
    YClientsBusinessError,
    YClientsConfigError,
    YClientsTimeoutError,
)

logger = logging.getLogger("services.integrations.yclients")

DEFAULT_BASE_URL = "https://api.yclients.com/api/v1"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5


def _setting(name: str, default=""):
    return getattr(settings, name, default) or default


class YClientsClient:
    """Read-only pull of services + staff for one YClients company.

    Credentials resolve from explicit args first, then settings
    (``YCLIENTS_PARTNER_TOKEN`` / ``YCLIENTS_USER_TOKEN`` /
    ``YCLIENTS_COMPANY_ID`` / ``YCLIENTS_API_BASE_URL``). A missing
    partner token or company id fails closed with
    :class:`YClientsConfigError` — the intake must not silently hit an
    unauthenticated or company-less endpoint.
    """

    def __init__(
        self,
        partner_token: str | None = None,
        user_token: str | None = None,
        company_id: int | str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_base: float = DEFAULT_BACKOFF_BASE,
    ):
        self.partner_token = (
            partner_token if partner_token is not None
            else _setting("YCLIENTS_PARTNER_TOKEN")
        )
        self.user_token = (
            user_token if user_token is not None
            else _setting("YCLIENTS_USER_TOKEN")
        )
        raw_company = (
            company_id if company_id is not None
            else _setting("YCLIENTS_COMPANY_ID", 0)
        )
        try:
            self.company_id = int(raw_company or 0)
        except (TypeError, ValueError):
            self.company_id = 0
        self.base_url = (base_url or _setting("YCLIENTS_API_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(
            timeout if timeout is not None
            else _setting("YCLIENTS_HTTP_TIMEOUT", DEFAULT_TIMEOUT)
        )
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base

        if not self.partner_token:
            raise YClientsConfigError("YCLIENTS_PARTNER_TOKEN is not set")
        if not self.company_id:
            raise YClientsConfigError("YCLIENTS_COMPANY_ID is not set")

    # -- HTTP -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        auth = f"Bearer {self.partner_token}"
        if self.user_token:
            auth += f", User {self.user_token}"
        return {
            "Authorization": auth,
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _is_transient(status: int) -> bool:
        return status == 429 or 500 <= status < 600

    def _get(self, path: str, params: dict | None = None) -> list | dict | None:
        """GET ``path`` and return the envelope ``data``.

        Retries transient failures (429 / 5xx / timeout) up to
        ``max_retries`` with exponential backoff. Raises typed errors on
        business (``success:false``) and non-retryable transport failures.
        """
        url = f"{self.base_url}{path}"
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.get(
                    url, headers=self._headers(), params=params,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise YClientsTimeoutError(last_error) from exc

            status = resp.status_code
            if self._is_transient(status):
                last_error = f"HTTP {status}"
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise YClientsAPIError(
                    f"transient HTTP {status} did not recover after "
                    f"{self.max_retries} retries"
                )

            return self._parse(resp)

        # Unreachable — loop either returns or raises. Kept for type-checkers.
        raise YClientsAPIError(last_error or "unknown YClients failure")

    def _parse(self, resp) -> list | dict | None:
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None

        if isinstance(body, dict) and body.get("success") is False:
            message = ""
            meta = body.get("meta")
            if isinstance(meta, dict):
                message = meta.get("message", "")
            if message:
                raise YClientsBusinessError(message, status_code=status)
            # success:false without a message on a 2xx/other status.
            if status in (401, 403):
                raise YClientsAuthError(f"HTTP {status}: authentication rejected")
            raise YClientsAPIError(f"HTTP {status}: success=false")

        if 200 <= status < 300:
            if isinstance(body, dict):
                return body.get("data")
            raise YClientsAPIError(f"HTTP {status}: non-object body")

        if status in (401, 403):
            raise YClientsAuthError(f"HTTP {status}: authentication rejected")
        raise YClientsAPIError(f"HTTP {status}: {str(resp.text)[:200]}")

    def _backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_base * (2 ** attempt)
        if delay > 0:
            time.sleep(delay)

    # -- public pull ----------------------------------------------------

    def list_services(self) -> list[dict]:
        """All services for the company (management endpoint, flat list).

        Needs the user token + an active salon licence. Returns the raw
        service dicts — normalization happens in the intake pipeline.
        """
        data = self._get(f"/company/{self.company_id}/services")
        return data if isinstance(data, list) else []

    def list_staff(self) -> list[dict]:
        """All bookable staff for the company (booking endpoint, list)."""
        data = self._get(f"/book_staff/{self.company_id}")
        return data if isinstance(data, list) else []
