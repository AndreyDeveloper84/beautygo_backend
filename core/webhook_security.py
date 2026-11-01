"""Shared source checks for provider webhooks (YooKassa).

``payments/views.py`` (PaymentWebhookView) and ``billing/webhooks.py``
(BillingYooKassaWebhookView) receive the same unsigned YooKassa callbacks
and used to carry two COPIES of the client-IP / allowlist / Basic-auth
helpers ("copied, not imported — stream isolation"). The copies drifted:
billing honoured ``X-Forwarded-For`` by position (N proxies from the
right) with no check that the direct peer was our own proxy, so a direct
caller could put any allowlisted address into the header and pass. One
implementation now — the payments one (amendment C trust model) — and
one policy for an empty allowlist.

Trust model (``client_ip``): forwarded headers are honoured ONLY when the
direct peer (``REMOTE_ADDR``) is one of ``YOOKASSA_WEBHOOK_TRUSTED_PROXY_IPS``
(loopback nginx / docker gateway by default). Then the LAST XFF entry is
the one our nginx appended — the truthful peer it saw; earlier entries
are client-controlled and ignored. From any other peer the header is
ignored entirely and the TCP-level address is checked.

Empty allowlist (``enforce_source_allowlist``): "allow all, warn" is a
development convenience. With ``YOOKASSA_WEBHOOK_ALLOWLIST_REQUIRED``
(prod.py sets it — production AND staging) an empty list is a refused
request, not an open door: the pilot's webhook must never be one unset
env var away from accepting a forged ``payment.succeeded``.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import logging

from django.conf import settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

logger = logging.getLogger(__name__)

_DEFAULT_TRUSTED_PROXIES = ["127.0.0.1", "::1", "172.16.0.0/12"]


def ip_in_allowlist(ip: str, allowlist: list[str]) -> bool:
    """Return True if *ip* matches any CIDR / single-IP entry in *allowlist*.

    An empty allowlist returns True — "not configured"; the CALLER decides
    whether that means permit (dev) or fail closed (prod). Malformed
    entries are logged and skipped so one bad line in the env var does
    not nuke webhook processing (settings validate them at load anyway).
    """
    if not allowlist:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning(
                "YOOKASSA_WEBHOOK_ALLOWED_IPS has invalid entry: %r", entry,
            )
    return False


def client_ip(request: Request) -> str:
    """Real client IP for the webhook allowlist (see module docstring)."""
    trusted = getattr(settings, "YOOKASSA_WEBHOOK_TRUSTED_PROXY_IPS", None)
    if trusted is None:
        trusted = _DEFAULT_TRUSTED_PROXIES
    remote = request.META.get("REMOTE_ADDR", "")
    if remote and ip_in_allowlist(remote, trusted):
        xff = [
            s.strip()
            for s in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if s.strip()
        ]
        if xff:
            return xff[-1]
    return remote


def enforce_source_allowlist(
    request: Request, *, log: logging.Logger = logger,
) -> str:
    """Raise ``PermissionDenied`` (403) unless the request's source passes
    ``YOOKASSA_WEBHOOK_ALLOWED_IPS``; return the resolved client IP.

    ``log`` lets each view keep its own logger name so existing log
    hygiene tests (which patch ``payments.views.logger``) keep seeing
    every line the view emits.
    """
    allowlist = getattr(settings, "YOOKASSA_WEBHOOK_ALLOWED_IPS", [])
    ip = client_ip(request)
    if not allowlist:
        if getattr(settings, "YOOKASSA_WEBHOOK_ALLOWLIST_REQUIRED", False):
            log.error(
                "YOOKASSA_WEBHOOK_ALLOWED_IPS is empty on a production-like "
                "deployment — webhook REJECTED (client_ip=%s). Empty means "
                "allow-all and is refused here; set the env var.",
                ip,
            )
            raise PermissionDenied("Webhook source allowlist is not configured.")
        log.warning(
            "YOOKASSA_WEBHOOK_ALLOWED_IPS unset — webhook accepts all "
            "sources (client_ip=%s). Set the env for defence in depth.",
            ip,
        )
        return ip
    if not ip_in_allowlist(ip, allowlist):
        log.warning(
            "Rejected YooKassa webhook from unexpected source %s", ip,
        )
        raise PermissionDenied("Source IP not allowed.")
    return ip


def verify_basic_auth(request: Request) -> bool:
    """Verify the ``Authorization: Basic`` header against the configured
    creds (amendment I). Comparison via ``hmac.compare_digest``
    (constant-time, no timing side-channel on user-controlled strings).

    Returns True when:
    - either env var (user OR pass) is empty (no auth configured — skip), OR
    - the header is present, well-formed AND creds match

    Returns False on: creds configured AND (header missing / scheme is not
    Basic (incl. Bearer) / undecodable Base64 / wrong pair). Every False
    maps to 401 at the call site — never a 500, never a 403.
    """
    expected_user = getattr(settings, "YOOKASSA_WEBHOOK_BASIC_AUTH_USER", "")
    expected_pass = getattr(settings, "YOOKASSA_WEBHOOK_BASIC_AUTH_PASS", "")
    if not expected_user or not expected_pass:
        return True

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False

    user_ok = hmac.compare_digest(user, expected_user)
    pass_ok = hmac.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def basic_auth_denied_response() -> Response:
    """401 for any Basic-auth failure (amendment I). The realm header is
    the RFC 7617 contract YooKassa's dashboard expects; the log line at
    the call site carries NO password, NO Authorization header and NO
    payload."""
    return Response(
        {"error": {"code": "UNAUTHORIZED", "message": "Invalid webhook credentials."}},
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="YooKassa webhook"'},
    )
