"""Webhook URL — mounted under the AppType-exempt internal prefix.

W1's urls patch (B-5) mounts this module at
``api/v1/internal/billing/`` → effective path
``/api/v1/internal/billing/webhook/``. The internal prefix is exempt
from AppTypeMiddleware (X-App-Type) — YooKassa is a server-to-server
caller and cannot send that header. Auth here is IP allowlist + Basic
auth + provider re-fetch, NOT the internal Bearer (see webhooks.py).
"""
from __future__ import annotations

from django.urls import path

from billing.webhooks import BillingYooKassaWebhookView


urlpatterns = [
    path(
        "webhook/",
        BillingYooKassaWebhookView.as_view(),
        name="billing-yookassa-webhook",
    ),
]
