"""DRF-2984 PR-C1 — billing switches, both off by default.

With ``BILLING_CHARGES_ENABLED`` off not one door of billing/ reaches
YooKassa, even with live-looking ``YOOKASSA_SHOP_ID/SECRET_KEY``: the SDK
is replaced by a spy that records every touch, and each door is driven
through its real entry point (HTTP view, beat task, service function).
Doors: card-setup, pay-debt, billing webhook, the shared payments webhook
router (billing branch), monthly charge task, dunning retry task, and the
five charge functions they sit on. Plus the startup guard.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import yookassa
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from rest_framework.test import APIClient

from billing import charges, tasks
from billing.flags import BillingChargesDisabled
from billing.models import (
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    SpecialistSubscription,
)
from billing.yookassa import BillingYooKassaClient

PROVIDER_ID = "yk_flags_2984"


@pytest.fixture(autouse=True)
def _billing_off(settings):
    # billing/tests/conftest.py switches billing on for the machinery
    # suites; this module pins the default — off.
    settings.BILLING_ENABLED = False
    settings.BILLING_CHARGES_ENABLED = False
    # Live-looking keys: the switch, not empty credentials, must stop it.
    settings.YOOKASSA_SHOP_ID = "123456"
    settings.YOOKASSA_SECRET_KEY = "live_looking_key"  # pragma: allowlist secret
    settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"


@pytest.fixture(autouse=True)
def sdk_spy(monkeypatch):
    """Every way into the YooKassa SDK billing/ uses, recorded."""
    spy = MagicMock(name="yookassa")
    monkeypatch.setattr(yookassa.Configuration, "configure", spy.configure)
    monkeypatch.setattr(yookassa, "Payment", spy.Payment)
    return spy


@pytest.fixture
def fake_client():
    """An injected client — the door must refuse before using it."""
    return MagicMock(name="BillingYooKassaClient")


@pytest.fixture
def api():
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer test-bearer")
    return client


@pytest.fixture
def open_invoice(db, due_subscription):
    return BillingInvoice.objects.create(
        subscription=due_subscription,
        period_start=date(2026, 7, 11),
        period_end=date(2026, 8, 10),
        subscription_amount=Decimal("690.00"),
        fees_amount=Decimal("0.00"),
        total_amount=Decimal("690.00"),
        idempotency_key=f"inv-{uuid.uuid4().hex[:8]}",
    )


@pytest.fixture
def pending_payment(db, open_invoice):
    return BillingPayment.objects.create(
        invoice=open_invoice,
        kind=BillingPayment.Kind.RECURRENT,
        amount=Decimal("690.00"),
        idempotency_key=f"bp-{uuid.uuid4().hex[:8]}",
        provider_payment_id=PROVIDER_ID,
    )


def _assert_no_sdk(spy):
    assert spy.mock_calls == [], f"YooKassa SDK touched: {spy.mock_calls}"


class TestDefaults:
    def test_both_switches_default_off(self):
        from djangoProject.settings import base

        assert base.BILLING_ENABLED is False
        assert base.BILLING_CHARGES_ENABLED is False


class TestStartupGuard:
    @pytest.mark.parametrize("enabled,charges,refuses", [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (False, True, True),
    ])
    def test_charges_without_billing_refuses_to_start(
        self, settings, enabled, charges, refuses,
    ):
        settings.BILLING_ENABLED = enabled
        settings.BILLING_CHARGES_ENABLED = charges
        config = apps.get_app_config("billing")
        if refuses:
            with pytest.raises(ImproperlyConfigured):
                config.ready()
        else:
            config.ready()


class TestHttpDoors:
    def test_card_setup_503_and_writes_nothing(self, api, specialist, sdk_spy):
        resp = api.post(
            f"/api/v1/internal/billing/specialists/{specialist.user_id}/card-setup/",
            {"tariff": "solo", "return_url": "https://t.me/ayla"},
            format="json",
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "BILLING_CHARGES_DISABLED"
        assert not SpecialistSubscription.objects.filter(
            user_id=specialist.user_id,
        ).exists()
        assert not BillingConsent.objects.exists()
        assert not BillingInvoice.objects.exists()
        _assert_no_sdk(sdk_spy)

    def test_pay_debt_503(self, api, specialist, open_invoice, sdk_spy):
        resp = api.post(
            f"/api/v1/internal/billing/specialists/{specialist.user_id}/pay-debt/",
            {"return_url": "https://t.me/ayla"},
            format="json",
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "BILLING_CHARGES_DISABLED"
        assert not open_invoice.payments.exists()
        _assert_no_sdk(sdk_spy)

    @pytest.mark.parametrize("url,headers", [
        ("/api/v1/internal/billing/webhook/", {}),
        ("/api/v1/payments/webhook/", {"HTTP_X_APP_TYPE": "client"}),
    ])
    def test_webhooks_ack_without_refetch(
        self, pending_payment, sdk_spy, url, headers,
    ):
        resp = APIClient().post(
            url,
            {"event": "payment.succeeded", "object": {"id": PROVIDER_ID}},
            format="json", **headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        pending_payment.refresh_from_db()
        assert pending_payment.status == BillingPayment.Status.PENDING
        _assert_no_sdk(sdk_spy)


class TestBeatDoors:
    @pytest.mark.parametrize("enabled", [False, True])
    def test_monthly_charge_is_noop(
        self, settings, due_subscription, sdk_spy, enabled,
    ):
        settings.BILLING_ENABLED = enabled
        assert tasks.charge_subscriptions_monthly() == 0
        assert not BillingInvoice.objects.exists()
        _assert_no_sdk(sdk_spy)

    @pytest.mark.parametrize("enabled", [False, True])
    def test_dunning_retry_is_noop(
        self, settings, open_invoice, sdk_spy, enabled,
    ):
        settings.BILLING_ENABLED = enabled
        sub = open_invoice.subscription
        sub.next_retry_at = sub.created_at
        sub.save(update_fields=["next_retry_at"])
        assert tasks.retry_failed_subscription_charges() == 0
        assert not open_invoice.payments.exists()
        _assert_no_sdk(sdk_spy)


class TestServiceDoors:
    """The functions under the views/tasks refuse even with an injected
    client — a future caller that skips the view cannot charge."""

    def test_start_card_setup(self, specialist_user, fake_client, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            charges.start_card_setup(
                user=specialist_user, tariff_code="solo",
                return_url="https://t.me/ayla", client=fake_client,
            )
        assert not SpecialistSubscription.objects.exists()
        assert fake_client.mock_calls == []
        _assert_no_sdk(sdk_spy)

    def test_charge_subscription(self, due_subscription, fake_client, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            charges.charge_subscription(
                subscription=due_subscription, client=fake_client,
            )
        assert not BillingInvoice.objects.exists()
        assert fake_client.mock_calls == []
        _assert_no_sdk(sdk_spy)

    def test_retry_open_invoice(self, open_invoice, fake_client, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            charges.retry_open_invoice(
                subscription=open_invoice.subscription, client=fake_client,
            )
        assert fake_client.mock_calls == []
        _assert_no_sdk(sdk_spy)

    def test_pay_debt(self, open_invoice, fake_client, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            charges.pay_debt(
                subscription=open_invoice.subscription,
                return_url="https://t.me/ayla", client=fake_client,
            )
        assert fake_client.mock_calls == []
        _assert_no_sdk(sdk_spy)

    def test_handle_webhook_event(self, pending_payment, fake_client, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            charges.handle_webhook_event(
                event="payment.succeeded", provider_payment_id=PROVIDER_ID,
                client=fake_client,
            )
        assert fake_client.mock_calls == []
        _assert_no_sdk(sdk_spy)

    def test_client_refuses_to_exist(self, sdk_spy):
        with pytest.raises(BillingChargesDisabled):
            BillingYooKassaClient()
        _assert_no_sdk(sdk_spy)


class TestSwitchedOnStillCharges:
    """The red-before for the gate: with both switches on the same spy
    IS reached — the spy is not blind."""

    def test_card_setup_reaches_sdk_when_on(
        self, settings, api, specialist, sdk_spy,
    ):
        settings.BILLING_ENABLED = True
        settings.BILLING_CHARGES_ENABLED = True
        sdk_spy.Payment.create.return_value = MagicMock(
            id="yk_on", status="pending",
            confirmation=MagicMock(confirmation_url="https://yoomoney.ru/x"),
        )
        resp = api.post(
            f"/api/v1/internal/billing/specialists/{specialist.user_id}/card-setup/",
            {"tariff": "solo", "return_url": "https://t.me/ayla"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert sdk_spy.Payment.create.called
