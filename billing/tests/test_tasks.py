"""Billing webhook view + beat tasks tests (provider always mocked)."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from billing.tasks import (
    charge_subscriptions_monthly,
    retry_failed_subscription_charges,
)
from billing.yookassa import BillingPaymentClientError


WEBHOOK_URL = "/api/v1/internal/billing/webhook/"


class TestBillingWebhookView:
    @pytest.fixture(autouse=True)
    def _open_security(self, settings):
        # The real .env may configure IP allowlist / basic auth — make
        # tests env-independent; each test sets its own where relevant.
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = ""
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = ""

    def _post(self, client, payload=None, **extra):
        return client.post(
            WEBHOOK_URL,
            payload or {"event": "payment.succeeded", "object": {"id": "yk_1"}},
            format="json",
            **extra,
        )

    def test_happy_path_delegates_to_handler(self, db):
        with patch(
            "billing.webhooks.handle_webhook_event", return_value="ok",
        ) as handler:
            resp = self._post(APIClient())
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        handler.assert_called_once_with(
            event="payment.succeeded", provider_payment_id="yk_1",
        )

    def test_duplicate_status_passed_through(self, db):
        with patch("billing.webhooks.handle_webhook_event", return_value="duplicate"):
            resp = self._post(APIClient())
        assert resp.json() == {"status": "duplicate"}

    def test_malformed_payload_ignored(self, db):
        resp = self._post(APIClient(), payload={"event": ""})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}

    def test_provider_error_acks_200(self, db):
        # Stop YooKassa retries on transient provider/API failures.
        with patch(
            "billing.webhooks.handle_webhook_event",
            side_effect=BillingPaymentClientError("boom"),
        ):
            resp = self._post(APIClient())
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_ip_allowlist_rejects_strangers(self, db, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        client = APIClient(REMOTE_ADDR="8.8.8.8")
        resp = self._post(client)
        assert resp.status_code == 403

    def test_ip_allowlist_cidr_match(self, db, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        client = APIClient(REMOTE_ADDR="185.71.76.10")
        with patch("billing.webhooks.handle_webhook_event", return_value="ok"):
            resp = self._post(client)
        assert resp.status_code == 200

    def test_basic_auth_enforced_when_configured(self, db, settings):
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = "ayla"
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = "secret"
        resp = self._post(APIClient())
        assert resp.status_code == 403


class TestBeatTasks:
    def test_monthly_charge_picks_only_due_with_card(
        self, db, due_subscription, subscription,
    ):
        # due_subscription: period ended + card → charged.
        # subscription: period ends 2026-07-31 (not due) → skipped.
        with patch(
            "billing.tasks.charge_subscription", return_value=object(),
        ) as charge:
            charged = charge_subscriptions_monthly()
        assert charged == 1
        _, kwargs = charge.call_args
        assert kwargs["subscription"].pk == due_subscription.pk

    def test_monthly_charge_survives_item_errors(self, db, due_subscription):
        with patch(
            "billing.tasks.charge_subscription", side_effect=RuntimeError("x"),
        ), patch("billing.tasks.sentry_sdk.capture_exception"):
            # No exception escapes; the batch completes.
            assert charge_subscriptions_monthly() == 0

    def test_dunning_retry_picks_due_retries(self, db, due_subscription):
        from django.utils import timezone

        due_subscription.next_retry_at = timezone.now() - timezone.timedelta(hours=1)
        due_subscription.failed_attempts = 1
        due_subscription.save(
            update_fields=["next_retry_at", "failed_attempts"],
        )
        with patch("billing.tasks.retry_open_invoice", return_value=True) as retry:
            retried = retry_failed_subscription_charges()
        assert retried == 1
        _, kwargs = retry.call_args
        assert kwargs["subscription"].pk == due_subscription.pk
