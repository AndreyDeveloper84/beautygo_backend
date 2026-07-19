"""YooKassa webhook receiver for billing payments.

POST /api/v1/internal/billing/webhook/ (mounted by W1's urls patch,
B-5 — under the AppType-exempt internal prefix because YooKassa is a
server-to-server caller that cannot send X-App-Type).

Defense-in-depth mirrors payments/views.py PaymentWebhookView (helpers
copied, not imported — stream isolation: W1 owns payments/):
1. Source-IP allowlist ``YOOKASSA_WEBHOOK_ALLOWED_IPS`` (YooKassa does
   not sign webhooks; it publishes source IP ranges).
2. Optional Basic Auth (user:pass in the webhook URL).
3. State re-fetch via the API — the payload is never trusted.
4. Status-based idempotency under select_for_update (replay-safe).
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import secrets

from django.conf import settings
from django.core.exceptions import PermissionDenied
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing.charges import handle_webhook_event
from billing.yookassa import BillingPaymentClientError, BillingPaymentConfigError

logger = logging.getLogger(__name__)


def _verify_basic_auth(request: Request) -> bool:
    """Constant-time Basic-auth check; skipped when env creds are unset."""
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
    return (
        secrets.compare_digest(user, expected_user)
        and secrets.compare_digest(password, expected_pass)
    )


def _client_ip(request: Request) -> str:
    """Real client IP behind N trusted proxies (see payments/views.py)."""
    trusted = max(1, getattr(settings, "YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT", 1))
    xff = [s.strip() for s in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if s.strip()]
    if len(xff) >= trusted:
        return xff[-trusted]
    return request.META.get("REMOTE_ADDR", "")


def _ip_in_allowlist(ip: str, allowlist: list[str]) -> bool:
    """Match ip against CIDR / single-IP entries; empty list = allow all."""
    if not allowlist:
        return True
    for entry in allowlist:
        try:
            if "/" in entry:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == entry:
                return True
        except ValueError:
            logger.warning("Skipping malformed allowlist entry %r", entry)
    return False


class BillingYooKassaWebhookView(APIView):
    """YooKassa webhook for subscription/setup payments (idempotent)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    # Reuse the existing scope — avoids a settings change (W1-owned).
    throttle_scope = "webhook_payment"

    @extend_schema(
        tags=["webhook", "billing"],
        request=inline_serializer(
            name="BillingYooKassaWebhookEvent",
            fields={
                "event": serializers.CharField(),
                "object": serializers.DictField(),
            },
        ),
        responses={200: inline_serializer(
            name="BillingYooKassaWebhookAck",
            fields={"status": serializers.CharField()},
        )},
    )
    def post(self, request: Request) -> Response:
        allowlist = getattr(settings, "YOOKASSA_WEBHOOK_ALLOWED_IPS", [])
        client_ip = _client_ip(request)
        if not allowlist:
            logger.warning(
                "YOOKASSA_WEBHOOK_ALLOWED_IPS unset — billing webhook "
                "accepts all sources (client_ip=%s).", client_ip,
            )
        elif not _ip_in_allowlist(client_ip, allowlist):
            logger.warning("Rejected billing webhook from %s", client_ip)
            raise PermissionDenied("Source IP not allowed.")

        if not _verify_basic_auth(request):
            logger.warning("Rejected billing webhook — invalid Basic Auth")
            raise PermissionDenied("Invalid credentials.")

        event = request.data.get("event")
        obj = request.data.get("object", {})
        provider_payment_id = obj.get("id", "")
        if not event or not provider_payment_id:
            return Response({"status": "ignored"}, status=200)

        try:
            status_ = handle_webhook_event(
                event=event, provider_payment_id=provider_payment_id,
            )
        except (BillingPaymentClientError, BillingPaymentConfigError) as exc:
            # 200 stops YooKassa retries; reconciliation job picks it up.
            logger.error("billing webhook verify failed: %s", exc)
            return Response({"status": "ok"}, status=200)
        return Response({"status": status_}, status=200)
