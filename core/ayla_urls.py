"""AylaUrlBuilder — single source for Ayla service URLs.

Handoff Block A → A4. The cross-service surface between Ayla and
bot-platform was assembled gradually: each new internal endpoint
hard-coded its base URL inline (sometimes the public host, sometimes
the internal cluster host, sometimes a literal IP for a quick fix).
By Phase 0 a single bot-platform call could land on three different
hostnames depending on which client built the request.

This module collapses that to one builder, settings-driven on both
sides of the integration. bot-platform mirrors the same class shape
in ``apps.integrations.ayla_*`` (cross-repo type-share decision lives
with W4 — see handoff). Both repos read the same env vars:

- ``AYLA_INTERNAL_BASE_URL`` — service-to-service calls
  (``/api/v1/payments/internal/``, ``/api/v1/masters/internal/``,
  ``/api/v1/internal/me/``). Behind the cluster firewall, no public
  TLS, Bearer-auth gated by ``AYLA_INTERNAL_API_TOKEN`` (A5).
- ``AYLA_PUBLIC_BASE_URL`` — endpoints reachable from the mobile
  app + the YooKassa ``return_url``. Public TLS, host matches
  ``DJANGO_ALLOWED_HOSTS``.

Why two bases? Internal services may run on a private host
(``ayla-api.internal:8000``) while customer-facing traffic terminates
at the load balancer (``api.ayla.app``). Conflating the two has
already caused a YooKassa webhook to be issued for an internal-only
host, which YooKassa cannot reach.

Usage:

    from core.ayla_urls import AylaUrlBuilder

    builder = AylaUrlBuilder.from_settings()
    url = builder.internal("/payments/internal/{id}/retry/", id=pk)
    url = builder.public("/payments/return/", token=t)

The builder rejects path arguments that already contain a scheme
(``raise ValueError``) — a literal ``https://…`` argument is exactly
the regression we are guarding against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urljoin

from django.conf import settings


_API_V1 = "/api/v1"


def _normalise_base(base: str) -> str:
    """Drop a trailing ``/`` so urljoin behaves consistently."""
    return base.rstrip("/") if base else ""


def _ensure_relative(path: str) -> str:
    """Reject paths that smuggle in a scheme. Forces callers to go
    through the builder for the host portion."""
    if "://" in path:
        raise ValueError(
            f"AylaUrlBuilder.path must be relative, got {path!r}. "
            "Pass only the path component (e.g. '/api/v1/...'); the "
            "builder owns the scheme + host."
        )
    if not path.startswith("/"):
        # Tolerate "v1/foo" — promote to "/v1/foo" so callers don't
        # have to remember the leading slash. urljoin needs it.
        path = "/" + path
    return path


@dataclass(frozen=True)
class AylaUrlBuilder:
    """Builds Ayla service URLs from two configured bases.

    ``internal_base`` and ``public_base`` are the only writable
    surface — everything else derives from them. Frozen dataclass
    so a mistake like ``builder.internal_base = "…"`` raises instead
    of silently mutating a singleton.
    """

    internal_base: str
    public_base: str

    @classmethod
    def from_settings(cls) -> "AylaUrlBuilder":
        """Construct from Django settings. Empty strings are tolerated
        at construct-time (dev / staging may not have both bases set);
        the builder methods raise when a caller actually tries to
        build a URL with an unconfigured base."""
        return cls(
            internal_base=_normalise_base(
                getattr(settings, "AYLA_INTERNAL_BASE_URL", "") or ""
            ),
            public_base=_normalise_base(
                getattr(settings, "AYLA_PUBLIC_BASE_URL", "") or ""
            ),
        )

    # --- builder methods -----------------------------------------------

    def internal(self, path: str, /, **path_kwargs: Any) -> str:
        """Build an internal-cluster URL.

        ``path`` may include ``{name}`` placeholders that are filled
        from ``path_kwargs`` using ``urllib.parse.quote`` so a UUID
        or arbitrary id cannot smuggle path-traversal sequences.
        ``query`` is reserved for future use — pass through
        ``with_query`` instead to keep call sites obvious.
        """
        if not self.internal_base:
            raise RuntimeError(
                "AYLA_INTERNAL_BASE_URL is unset. Set it in env before "
                "calling AylaUrlBuilder.internal()."
            )
        return self._build(self.internal_base, path, path_kwargs)

    def public(self, path: str, /, **path_kwargs: Any) -> str:
        """Build a public-host URL (used by mobile + YooKassa return_url)."""
        if not self.public_base:
            raise RuntimeError(
                "AYLA_PUBLIC_BASE_URL is unset. Set it in env before "
                "calling AylaUrlBuilder.public()."
            )
        return self._build(self.public_base, path, path_kwargs)

    def with_query(self, url: str, /, **query: Any) -> str:
        """Append a ``?key=value&...`` query string to an existing URL.

        Empty / None values are dropped — they would otherwise serialise
        as ``key=`` which most receivers parse as the literal empty
        string, masking a missing-context bug as an empty filter.
        """
        if not query:
            return url
        filtered = {k: v for k, v in query.items() if v not in (None, "")}
        if not filtered:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urlencode(filtered, doseq=True)}"

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _build(base: str, path: str, path_kwargs: dict[str, Any]) -> str:
        path = _ensure_relative(path)
        if path_kwargs:
            quoted = {k: quote(str(v), safe="") for k, v in path_kwargs.items()}
            path = path.format(**quoted)
        # urljoin handles the slash boundary between base and path.
        # base is already normalised (no trailing /), path is leading-/.
        return urljoin(base + "/", path.lstrip("/"))

    # --- convenience shortcuts for the most common surfaces ------------

    def api_v1(self, suffix: str, /, **kwargs: Any) -> str:
        """Build an ``{internal_base}/api/v1{suffix}`` URL. Most internal
        callers want this — fewer string-concat opportunities means
        fewer drift bugs."""
        return self.internal(f"{_API_V1}{_ensure_relative(suffix)}", **kwargs)

    def public_api_v1(self, suffix: str, /, **kwargs: Any) -> str:
        """Public analogue of ``api_v1`` — used for ``return_url`` and
        mobile-facing endpoints."""
        return self.public(f"{_API_V1}{_ensure_relative(suffix)}", **kwargs)
