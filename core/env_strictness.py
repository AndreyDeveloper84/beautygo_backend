"""Production env-strictness gate.

prod.py runs the same Django settings module on real production *and*
on staging / dev VPS environments that mirror the prod stack. Without
gating, any missing OAuth / webhook env raises ``ImproperlyConfigured``
at import — useful in real production (catches a misconfigured deploy
before traffic flows) but blocks a staging VPS from booting just
because it doesn't have real OAuth credentials yet.

``DJANGO_ENV`` separates the two:

- ``production`` (default) — strict; missing values raise.
- anything else (``staging``, ``dev``, …) — warn-and-boot; the same
  values are still expected eventually but their absence doesn't
  abort startup.

The default is ``production`` so a deploy that forgets the var is
treated as the strict case (fail-closed).
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("django.security")


def is_strict_production() -> bool:
    """Return ``True`` when the current process should treat missing
    security-critical env as fatal."""
    label = os.environ.get("DJANGO_ENV", "production").strip().lower()
    return label == "production"


def enforce_required_env(missing: list[str], purpose: str) -> None:
    """Either raise (strict prod) or warn (non-prod), based on the
    ``DJANGO_ENV`` label.

    ``missing`` is the list of env-var names that are unset/empty;
    ``purpose`` describes what defence is degraded so the warning
    has actionable context.
    """
    if not missing:
        return
    detail = f"missing {', '.join(missing)}: {purpose}"
    if is_strict_production():
        raise ImproperlyConfigured(
            f"Production requires these security-critical env vars: {detail}"
        )
    logger.warning(
        "DJANGO_ENV=%s — degraded security: %s. Set these before promoting "
        "this environment to production.",
        os.environ.get("DJANGO_ENV", "(unset)"),
        detail,
    )


# --- structural URL validation (DRF-1244) ------------------------------
#
# ``enforce_required_env`` above answers "is the variable set?". It does
# not answer "is the value usable?" — and for a URL those are different
# questions. ``AYLA_PUBLIC_BASE_URL=да``, ``…=TODO`` or a half-pasted
# ``…=https://`` all satisfy presence. They fail at the first call that
# builds a URL from them, which in practice means in production, on a
# real user's booking, hours after the deploy went green.
#
# The check below moves that failure to boot: a malformed URL aborts the
# process the same way a missing one does, under the same ``DJANGO_ENV``
# gate (strict production raises, staging / dev warn-and-boot).
#
# Deliberately *structural*, not reachability: no DNS, no TCP, nothing
# that could make startup depend on the network. It answers exactly one
# question — could ``urljoin``/``requests`` do anything sane with this
# string?

_ALLOWED_URL_SCHEMES = ("http", "https")

# Shown in every error message so the operator sees the target shape
# without having to look up the docs at 04:00.
_URL_SHAPE_HINT = "an absolute URL like https://host[:port][/path]"


def describe_url_defect(value: str | None) -> str | None:
    """Return a human description of why ``value`` is not a usable
    absolute http(s) URL, or ``None`` when it is fine.

    The description NEVER contains the value itself — these variables sit
    next to credentials in ``.env`` and the message ends up in logs,
    Sentry and screenshots. It describes the *shape* (missing scheme,
    empty host) and, for the scheme, the offending scheme name, which is
    not secret and is the single most useful token for diagnosis.
    """
    if value is None:
        return "is not set"
    if not value.strip():
        return f"is set but empty — expected {_URL_SHAPE_HINT}"
    if any(char.isspace() for char in value):
        # Covers the classic .env accidents: a trailing space after the
        # value, a line wrapped mid-URL, a copy-paste that took the
        # surrounding prose with it. requests would send the space
        # percent-encoded or reject the URL outright.
        return (
            "contains whitespace — expected "
            f"{_URL_SHAPE_HINT} with no spaces or line breaks"
        )

    try:
        parts = urlsplit(value)
    except ValueError as exc:  # e.g. an unclosed IPv6 bracket
        return f"is not parseable as a URL ({exc.__class__.__name__}: {exc})"

    if not parts.scheme:
        # "да", "TODO", "api.gobeauty.site", "/api/v1" all land here.
        return (
            "has no scheme — expected "
            f"{_URL_SHAPE_HINT}, not a bare host or a path"
        )
    if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        return (
            f"uses the {parts.scheme!r} scheme — only "
            f"{' and '.join(s + '://' for s in _ALLOWED_URL_SCHEMES)} "
            "are supported here"
        )
    if not parts.netloc:
        # The truncated "https://" paste, and "http:///api/v1".
        return f"has a scheme but no host — expected {_URL_SHAPE_HINT}"
    try:
        hostname = parts.hostname
    except ValueError as exc:  # malformed port / IPv6 literal
        return f"has an unusable host ({exc.__class__.__name__}: {exc})"
    if not hostname:
        # netloc that is only credentials or only a port: "https://:8000",
        # "https://user@".
        return f"has an empty host — expected {_URL_SHAPE_HINT}"
    try:
        parts.port  # raises ValueError on a non-numeric / out-of-range port
    except ValueError as exc:
        return f"has an invalid port ({exc})"
    return None


def is_structurally_valid_http_url(value: str | None) -> bool:
    """``True`` when ``value`` is an absolute http(s) URL with a host.

    Structural only — says nothing about whether the host resolves or
    answers. See ``describe_url_defect`` for the reason when ``False``.
    """
    return describe_url_defect(value) is None


def enforce_url_env(names: tuple[str, ...] | list[str], purpose: str) -> None:
    """Validate the *form* of every env var in ``names`` that is set.

    Absence is NOT an error here — presence is a separate policy owned by
    ``enforce_required_env`` / the caller. Adding a presence requirement
    through this function would silently widen the set of variables a
    deploy must provide and could stop a working environment from
    booting. This function only says: if you set it, it has to be a URL.

    A blank value counts as absence. Every consumer of these settings
    reads them as ``os.environ.get(NAME, "")`` and branches on
    ``if not value`` — empty is the documented off-switch (``publisher``
    no-ops, ``OPENAI_BASE_URL`` falls back to the OpenAI default,
    ``AylaUrlBuilder`` raises only when a caller actually needs the
    base). Treating ``NAME=`` as a malformed URL would turn a supported
    "feature off" configuration into a failed boot. Whether any of these
    must be *non-empty* in production is a presence decision and belongs
    in ``_REQUIRED_PROD_ENV``, not here.

    Same ``DJANGO_ENV`` gate as ``enforce_required_env`` — strict
    production raises ``ImproperlyConfigured``, everything else warns and
    boots.
    """
    defects = []
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue  # unset / explicitly blank — not this function's business
        defect = describe_url_defect(raw)
        if defect is not None:
            defects.append(f"{name} {defect}")
    if not defects:
        return

    detail = "; ".join(defects)
    if is_strict_production():
        raise ImproperlyConfigured(
            f"Production requires well-formed URLs in these env vars: "
            f"{detail}. {purpose}"
        )
    logger.warning(
        "DJANGO_ENV=%s — malformed URL configuration: %s. %s Fix these "
        "before promoting this environment to production; on "
        "DJANGO_ENV=production the same values abort startup.",
        os.environ.get("DJANGO_ENV", "(unset)"),
        detail,
        purpose,
    )
