"""YooKassa webhook receiver for billing payments.

POST /api/v1/internal/billing/webhook/ (mounted by W1's urls patch,
B-5 — under the AppType-exempt internal prefix because YooKassa is a
server-to-server caller that cannot send X-App-Type).

Defense-in-depth mirrors payments/views.py PaymentWebhookView. The
source checks are SHARED (core.webhook_security), not copied: the copy
that lived here trusted X-Forwarded-For by position (N proxies from the
right) without checking that the direct peer was our own proxy, so a
direct caller could spoof an allowlisted address into the header.
1. Source-IP allowlist ``YOOKASSA_WEBHOOK_ALLOWED_IPS`` (YooKassa does
   not sign webhooks; it publishes source IP ranges). XFF is honoured
   only from ``YOOKASSA_WEBHOOK_TRUSTED_PROXY_IPS``; an empty allowlist
   is refused when ``YOOKASSA_WEBHOOK_ALLOWLIST_REQUIRED`` (prod/staging).
2. Optional Basic Auth (user:pass in the webhook URL) — 401 + realm on
   failure, like the payments webhook.
3. State re-fetch via the API — the payload is never trusted.
4. Status-based idempotency under select_for_update (replay-safe).
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from billing.charges import handle_webhook_event
from billing.flags import BillingChargesDisabled
from billing.yookassa import BillingPaymentClientError, BillingPaymentConfigError
from core.webhook_security import (
    basic_auth_denied_response,
    enforce_source_allowlist,
    verify_basic_auth,
)

logger = logging.getLogger(__name__)


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
        # 403 on a non-allowlisted peer, or on an empty allowlist where
        # YOOKASSA_WEBHOOK_ALLOWLIST_REQUIRED (prod/staging).
        enforce_source_allowlist(request, log=logger)

        # 401 + WWW-Authenticate, never 403: the same contract as the
        # payments webhook. No password / header / payload in the log.
        if not verify_basic_auth(request):
            logger.warning("Rejected billing webhook — invalid Basic Auth")
            return basic_auth_denied_response()

        event = request.data.get("event")
        obj = request.data.get("object", {})
        provider_payment_id = obj.get("id", "")
        if not event or not provider_payment_id:
            return Response({"status": "ignored"}, status=200)

        try:
            status_ = handle_webhook_event(
                event=event, provider_payment_id=provider_payment_id,
            )
        except BillingChargesDisabled:
            # DRF-2984: with charges off billing/ creates no payments, so
            # an event here is stray — ack it, loudly, without re-fetching.
            logger.error(
                "billing webhook ignored: charges disabled (event=%s id=%s)",
                event, provider_payment_id,
            )
            return Response({"status": "ignored"}, status=200)
        except (BillingPaymentClientError, BillingPaymentConfigError) as exc:
            # 200 stops YooKassa retries; reconciliation job picks it up.
            logger.error("billing webhook verify failed: %s", exc)
            return Response({"status": "ok"}, status=200)
        return Response({"status": status_}, status=200)
