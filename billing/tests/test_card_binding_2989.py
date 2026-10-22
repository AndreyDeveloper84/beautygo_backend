"""DRF-2989 — в пробном месяце карта привязывается без списания (В-10, В3).

* в пробном card-setup не выставляет счёт: холд 1 ₽ capture=False с
  save_payment_method; период, статус и события подписки не трогаются;
* ``waiting_for_capture`` сохраняет карту и снимает холд (cancel), ничего
  не списано; повтор вебхука — duplicate, второй отмены нет;
* первое настоящее списание — ``charge_subscription`` в ``trial_ends_at``;
* незавершённая привязка/оплата отдаётся как есть в любой день — второй
  платёж не открывается (ключ по дню больше не разводит);
* вне пробного (frozen после него, старый личный счёт) — как прежде.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from appointments.models import OutboxEvent
from billing.charges import (
    charge_subscription,
    handle_webhook_event,
    start_card_setup,
    start_trial,
)
from billing.models import (
    BillingInvoice,
    BillingPayment,
    SpecialistSubscription,
    TariffPlan,
)
from billing.tests.test_charges import FakeYooKassaClient

pytestmark = pytest.mark.django_db

URL = "https://miniapp.example/billing"


@pytest.fixture
def trial(specialist_user, tenant):
    sub = SpecialistSubscription.objects.create(
        user=specialist_user, tenant=tenant,
        tariff=TariffPlan.objects.get(code="salon"),
        status=SpecialistSubscription.Status.TRIAL,
    )
    return start_trial(sub, started_on=timezone.localdate())


def _setup(user, tenant, client):
    return start_card_setup(
        user=user, tariff_code="salon", tenant=tenant, return_url=URL, client=client,
    )


def _topics(*topics):
    return OutboxEvent.objects.filter(topic__in=topics).count()


def _authorise(payment, capture, client=None):
    client = client or FakeYooKassaClient(
        status="waiting_for_capture", method_id="pm_trial_1",
        card_last4="1111", card_brand="MasterCard",
    )
    with capture(execute=True):
        result = handle_webhook_event(
            event="payment.waiting_for_capture",
            provider_payment_id=payment.provider_payment_id, client=client,
        )
    return result, client


class TestBindingInTrial:
    def test_no_invoice_hold_of_one_ruble_without_capture(self, specialist_user, tenant, trial):
        period = (trial.current_period_start, trial.current_period_end)
        client = FakeYooKassaClient()

        result = _setup(specialist_user, tenant, client)

        assert result.invoice_id is None
        assert BillingInvoice.objects.count() == 0
        assert client.setup_calls == []
        call = client.binding_calls[0]
        assert call["amount"] == Decimal("1.00")
        payment = BillingPayment.objects.get()
        assert payment.kind == BillingPayment.Kind.CARD_BINDING
        assert payment.subscription == trial
        assert payment.invoice is None
        assert result.confirmation_url == payment.confirmation_url
        trial.refresh_from_db()
        assert trial.status == SpecialistSubscription.Status.TRIAL
        assert (trial.current_period_start, trial.current_period_end) == period

    def test_the_client_asks_for_a_hold_not_a_capture(self):
        from billing.yookassa import BillingYooKassaClient

        sent = {}

        class _Payment:
            id = "yk_1"
            status = "pending"
            confirmation = type("C", (), {"confirmation_url": "https://pay"})()

        class _Cls:
            @staticmethod
            def create(payload, key):
                sent.update(payload)
                return _Payment()

        client = BillingYooKassaClient.__new__(BillingYooKassaClient)
        client._payment_cls = _Cls
        client.create_binding_hold(
            amount=Decimal("1.00"), description="d", return_url=URL,
            idempotency_key="k", receipt=None, metadata={},
        )
        assert sent["capture"] is False
        assert sent["save_payment_method"] is True
        assert sent["amount"] == {"value": "1.00", "currency": "RUB"}

    def test_authorised_hold_saves_the_card_and_is_released(
        self, specialist_user, tenant, trial, django_capture_on_commit_callbacks,
    ):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        payment = BillingPayment.objects.get()
        period = (trial.current_period_start, trial.current_period_end)

        result, client = _authorise(payment, django_capture_on_commit_callbacks)

        assert result == "ok"
        trial.refresh_from_db()
        assert trial.payment_method_id == "pm_trial_1"
        assert trial.card_last4 == "1111"
        assert trial.status == SpecialistSubscription.Status.TRIAL
        assert (trial.current_period_start, trial.current_period_end) == period
        assert trial.failed_attempts == 0
        assert client.cancel_calls == [
            (payment.provider_payment_id, f"cancel:{payment.idempotency_key}"),
        ]
        payment.refresh_from_db()
        assert payment.status == BillingPayment.Status.SUCCEEDED
        assert _topics("subscription.activated", "subscription.renewed") == 0

    def test_redelivered_webhook_is_a_duplicate_without_second_cancel(
        self, specialist_user, tenant, trial, django_capture_on_commit_callbacks,
    ):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        payment = BillingPayment.objects.get()
        _authorise(payment, django_capture_on_commit_callbacks)

        again, client = _authorise(payment, django_capture_on_commit_callbacks)

        assert again == "duplicate"
        assert client.cancel_calls == []

    def test_release_failure_does_not_fail_the_webhook(
        self, specialist_user, tenant, trial, django_capture_on_commit_callbacks, caplog,
    ):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        payment = BillingPayment.objects.get()

        class _CancelDown(FakeYooKassaClient):
            def cancel_payment(self, *a, **kw):
                raise RuntimeError("provider down")

        client = _CancelDown(status="waiting_for_capture", method_id="pm_trial_1")
        with caplog.at_level(logging.ERROR, logger="billing.card_binding"):
            result, _ = _authorise(payment, django_capture_on_commit_callbacks, client)

        assert result == "ok"
        trial.refresh_from_db()
        assert trial.payment_method_id == "pm_trial_1"
        assert "billing.card_binding.release_failed" in caplog.text

    def test_abandoned_binding_is_canceled_without_dunning(self, specialist_user, tenant, trial):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        payment = BillingPayment.objects.get()

        result = handle_webhook_event(
            event="payment.canceled", provider_payment_id=payment.provider_payment_id,
            client=FakeYooKassaClient(status="canceled"),
        )

        assert result == "ok"
        payment.refresh_from_db()
        assert payment.status == BillingPayment.Status.CANCELED
        trial.refresh_from_db()
        assert trial.failed_attempts == 0
        assert trial.next_retry_at is None
        assert trial.status == SpecialistSubscription.Status.TRIAL
        # A fresh attempt opens a new hold with the next key.
        client = FakeYooKassaClient()
        _setup(specialist_user, tenant, client)
        assert BillingPayment.objects.filter(
            kind=BillingPayment.Kind.CARD_BINDING,
            idempotency_key=f"bind:{trial.id}:attempt-2",
        ).exists()

    def test_a_captured_hold_is_loud_and_settles_nothing(
        self, specialist_user, tenant, trial, caplog,
    ):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        payment = BillingPayment.objects.get()

        with caplog.at_level(logging.CRITICAL, logger="billing.card_binding"):
            result = handle_webhook_event(
                event="payment.succeeded", provider_payment_id=payment.provider_payment_id,
                client=FakeYooKassaClient(status="succeeded"),
            )

        assert result == "ignored"
        assert "billing.card_binding.captured" in caplog.text
        trial.refresh_from_db()
        assert trial.status == SpecialistSubscription.Status.TRIAL
        assert trial.payment_method_id == ""

    def test_cabinet_before_publication_is_bound_without_a_charge(self, specialist_user, tenant):
        result = _setup(specialist_user, tenant, FakeYooKassaClient())

        assert result.invoice_id is None
        assert BillingInvoice.objects.count() == 0


class TestFirstChargeIsDeferredToTrialEnd:
    def test_no_charge_inside_the_trial_then_charged_on_trial_end(
        self, specialist_user, tenant, trial, django_capture_on_commit_callbacks,
    ):
        _setup(specialist_user, tenant, FakeYooKassaClient())
        _authorise(BillingPayment.objects.get(), django_capture_on_commit_callbacks)
        trial.refresh_from_db()
        client = FakeYooKassaClient(status="succeeded")

        assert charge_subscription(
            subscription=trial, today=trial.trial_ends_at - timedelta(days=1), client=client,
        ) is None
        assert client.recurrent_calls == []

        invoice = charge_subscription(subscription=trial, today=trial.trial_ends_at, client=client)

        assert invoice is not None
        assert invoice.period_start == trial.trial_ends_at
        assert client.recurrent_calls[0]["payment_method_id"] == "pm_trial_1"
        assert client.recurrent_calls[0]["amount"] == Decimal("990.00")
        trial.refresh_from_db()
        assert trial.status == SpecialistSubscription.Status.ACTIVE


class TestOneUnfinishedPaymentAtATime:
    def test_pending_binding_is_returned_on_another_day(self, specialist_user, tenant, trial):
        client = FakeYooKassaClient()
        first = _setup(specialist_user, tenant, client)
        tomorrow = timezone.localdate() + timedelta(days=1)

        with patch("billing.charges.timezone.localdate", return_value=tomorrow):
            second = _setup(specialist_user, tenant, client)

        assert second.confirmation_url == first.confirmation_url
        assert len(client.binding_calls) == 1
        assert BillingPayment.objects.count() == 1

    def test_pending_setup_payment_is_returned_on_another_day(self, specialist_user):
        client = FakeYooKassaClient()
        first = start_card_setup(
            user=specialist_user, tariff_code="solo", return_url=URL, client=client,
        )
        tomorrow = timezone.localdate() + timedelta(days=1)

        with patch("billing.charges.timezone.localdate", return_value=tomorrow):
            second = start_card_setup(
                user=specialist_user, tariff_code="solo", return_url=URL, client=client,
            )

        assert second.invoice_id == first.invoice_id
        assert second.confirmation_url == first.confirmation_url
        assert len(client.setup_calls) == 1
        assert BillingInvoice.objects.count() == 1


class TestOutsideTheTrialAsBefore:
    def test_frozen_after_trial_pays_the_period(self, specialist_user, tenant, trial):
        SpecialistSubscription.objects.filter(pk=trial.pk).update(
            status=SpecialistSubscription.Status.FROZEN,
            frozen_reason="trial_ended_no_card",
        )
        client = FakeYooKassaClient()

        result = _setup(specialist_user, tenant, client)

        assert result.invoice_id is not None
        assert client.binding_calls == []
        assert client.setup_calls[0]["amount"] == Decimal("990.00")

    def test_trial_ending_today_is_not_a_binding(self, specialist_user, tenant, trial):
        SpecialistSubscription.objects.filter(pk=trial.pk).update(
            trial_ends_at=timezone.localdate(),
        )
        client = FakeYooKassaClient()

        result = _setup(specialist_user, tenant, client)

        assert result.invoice_id is not None
        assert client.binding_calls == []
