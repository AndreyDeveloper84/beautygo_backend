"""Wave-2 money orchestration tests — YooKassa is ALWAYS faked here."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from billing.charges import (
    charge_subscription,
    handle_webhook_event,
    register_charge_failure,
    retry_open_invoice,
    start_card_setup,
)
from billing.models import (
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    BookingFee,
    SpecialistSubscription,
)
from billing.yookassa import BillingPaymentClientError


class FakeYooKassaClient:
    """Test double for BillingYooKassaClient (records calls, no network)."""

    def __init__(self, status="succeeded", method_id="pm_saved_1", method_saved=True):
        self.status = status
        self.method_id = method_id
        self.method_saved = method_saved
        self.setup_calls = []
        self.recurrent_calls = []
        self._counter = 0

    def create_setup_payment(self, **kwargs):
        self.setup_calls.append(kwargs)
        self._counter += 1
        return {
            "provider_payment_id": f"yk_setup_{self._counter}",
            "confirmation_url": "https://pay.example/confirm",
            "status": "pending",
        }

    def create_recurrent_payment(self, **kwargs):
        self.recurrent_calls.append(kwargs)
        self._counter += 1
        return {
            "provider_payment_id": f"yk_rec_{self._counter}",
            "status": self.status,
        }

    def get_payment_info(self, provider_payment_id):
        return {
            "provider_payment_id": provider_payment_id,
            "status": self.status,
            "paid": self.status == "succeeded",
            "payment_method_id": self.method_id,
            "payment_method_saved": self.method_saved,
        }


class RaisingYooKassaClient(FakeYooKassaClient):
    def create_recurrent_payment(self, **kwargs):
        raise BillingPaymentClientError("network down")


def _fees(subscription, appointment, count_amount_pairs):
    fees = []
    for appt, amount in count_amount_pairs:
        fees.append(BookingFee.objects.create(
            appointment=appt, subscription=subscription,
            amount=amount, period_start=date(2026, 7, 1),
        ))
    return fees


class TestStartCardSetup:
    def test_creates_everything_and_returns_confirmation_url(
        self, db, specialist_user,
    ):
        client = FakeYooKassaClient()
        result = start_card_setup(
            user=specialist_user, tariff_code="solo",
            return_url="https://miniapp.example/billing",
            client=client,
        )
        subscription = SpecialistSubscription.objects.get(user=specialist_user)
        assert result.subscription_id == subscription.id
        assert result.confirmation_url == "https://pay.example/confirm"
        assert subscription.status == SpecialistSubscription.Status.TRIAL

        invoice = subscription.invoices.get()
        assert invoice.total_amount == Decimal("690.00")
        assert invoice.period_start == timezone.localdate()

        payment = invoice.payments.get()
        assert payment.kind == BillingPayment.Kind.SETUP
        assert payment.idempotency_key == f"pay:{invoice.idempotency_key}"

        # D7 consent recorded with the (placeholder) offer version.
        consent = BillingConsent.objects.get(user=specialist_user)
        assert "todo-legal" in consent.document_version  # TODO(legal) B-6

        # Provider call args: amount + 54-ФЗ receipt with the payer.
        call = client.setup_calls[0]
        assert call["receipt"]["customer"]["phone"] == "+79990000020"
        assert call["receipt"]["items"][0]["amount"]["value"] == "690.00"

    def test_repeat_call_is_idempotent(self, db, specialist_user):
        client = FakeYooKassaClient()
        first = start_card_setup(
            user=specialist_user, tariff_code="solo",
            return_url="https://miniapp.example/billing", client=client,
        )
        second = start_card_setup(
            user=specialist_user, tariff_code="solo",
            return_url="https://miniapp.example/billing", client=client,
        )
        assert first.invoice_id == second.invoice_id
        assert first.confirmation_url == second.confirmation_url
        assert len(client.setup_calls) == 1
        assert BillingInvoice.objects.count() == 1
        assert BillingPayment.objects.count() == 1

    def test_salon_binds_tenant_account(self, db, specialist_user, tenant):
        result = start_card_setup(
            user=specialist_user, tariff_code="salon", tenant=tenant,
            return_url="https://miniapp.example/billing",
            client=FakeYooKassaClient(),
        )
        subscription = SpecialistSubscription.objects.get(pk=result.subscription_id)
        assert subscription.tenant_id == tenant.id
        assert subscription.tariff.code == "salon"


class TestSetupWebhook:
    def _setup_payment(self, specialist_user):
        client = FakeYooKassaClient()
        start_card_setup(
            user=specialist_user, tariff_code="solo",
            return_url="https://miniapp.example/billing", client=client,
        )
        return BillingPayment.objects.get(kind=BillingPayment.Kind.SETUP)

    def test_success_activates_and_saves_method(self, db, specialist_user):
        payment = self._setup_payment(specialist_user)
        client = FakeYooKassaClient(status="succeeded", method_id="pm_card_42")
        with patch("billing.events.emit_subscription_activated") as emit:
            result = handle_webhook_event(
                event="payment.succeeded",
                provider_payment_id=payment.provider_payment_id,
                client=client,
            )
        assert result == "ok"
        payment.refresh_from_db()
        subscription = SpecialistSubscription.objects.get(user=specialist_user)
        assert payment.status == BillingPayment.Status.SUCCEEDED
        assert subscription.payment_method_id == "pm_card_42"
        assert subscription.status == SpecialistSubscription.Status.ACTIVE
        assert subscription.invoices.get().status == BillingInvoice.Status.PAID
        emit.assert_called_once()

    def test_replay_is_duplicate(self, db, specialist_user):
        payment = self._setup_payment(specialist_user)
        client = FakeYooKassaClient(status="succeeded")
        with patch("billing.events.emit_subscription_activated") as emit:
            handle_webhook_event(
                event="payment.succeeded",
                provider_payment_id=payment.provider_payment_id, client=client,
            )
            again = handle_webhook_event(
                event="payment.succeeded",
                provider_payment_id=payment.provider_payment_id, client=client,
            )
        assert again == "duplicate"
        emit.assert_called_once()  # activation fires exactly once

    def test_unknown_payment_acks_silently(self, db):
        result = handle_webhook_event(
            event="payment.succeeded", provider_payment_id="yk_unknown",
            client=FakeYooKassaClient(),
        )
        assert result == "ok"

    def test_canceled_registers_dunning_failure(self, db, specialist_user):
        payment = self._setup_payment(specialist_user)
        subscription = SpecialistSubscription.objects.get(user=specialist_user)
        result = handle_webhook_event(
            event="payment.canceled",
            provider_payment_id=payment.provider_payment_id,
            client=FakeYooKassaClient(status="canceled"),
        )
        assert result == "ok"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == BillingPayment.Status.FAILED
        assert subscription.failed_attempts == 1
        assert subscription.next_retry_at is not None


class TestChargeSubscription:
    def test_not_due_yet(self, db, subscription_with_card):
        client = FakeYooKassaClient()
        assert charge_subscription(
            subscription=subscription_with_card,
            today=date(2026, 7, 15),  # period ends 2026-07-31
            client=client,
        ) is None
        assert client.recurrent_calls == []

    def test_no_saved_card(self, db, subscription):
        subscription.current_period_end = date(2026, 7, 10)
        subscription.save(update_fields=["current_period_end"])
        assert charge_subscription(
            subscription=subscription, today=date(2026, 7, 19),
            client=FakeYooKassaClient(),
        ) is None

    def test_past_due_not_charged(self, db, due_subscription):
        due_subscription.status = SpecialistSubscription.Status.PAST_DUE
        due_subscription.save(update_fields=["status"])
        assert charge_subscription(
            subscription=due_subscription, today=date(2026, 7, 19),
            client=FakeYooKassaClient(),
        ) is None

    def test_happy_path_collects_fees_and_advances_period(
        self, db, due_subscription, appointment, specialist, service,
    ):
        _fees(due_subscription, appointment, [(appointment, Decimal("90.00"))])
        client = FakeYooKassaClient(status="succeeded")
        with patch("billing.events.emit_fee_charged"):
            invoice = charge_subscription(
                subscription=due_subscription, today=date(2026, 7, 19),
                client=client,
            )
        assert invoice is not None
        invoice.refresh_from_db()
        due_subscription.refresh_from_db()

        assert invoice.subscription_amount == Decimal("690.00")
        assert invoice.fees_amount == Decimal("90.00")
        assert invoice.total_amount == Decimal("780.00")
        assert invoice.status == BillingInvoice.Status.PAID
        assert invoice.period_start == date(2026, 7, 11)

        # Period advanced to the invoice period.
        assert due_subscription.current_period_start == date(2026, 7, 11)
        assert due_subscription.current_period_end == date(2026, 8, 10)

        fee = BookingFee.objects.get(appointment=appointment)
        assert fee.status == BookingFee.Status.CHARGED
        assert fee.invoice_id == invoice.id

        call = client.recurrent_calls[0]
        assert call["payment_method_id"] == "pm_test_123"
        assert call["amount"] == Decimal("780.00")
        assert "attempt-1" in call["idempotency_key"]

    def test_existing_invoice_not_recharged_by_sweep(self, db, due_subscription):
        client = FakeYooKassaClient(status="succeeded")
        first = charge_subscription(
            subscription=due_subscription, today=date(2026, 7, 19), client=client,
        )
        assert first is not None
        # Monthly sweep next day: invoice exists → no second charge.
        again = charge_subscription(
            subscription=due_subscription, today=date(2026, 7, 20), client=client,
        )
        assert again is None
        assert len(client.recurrent_calls) == 1

    def test_provider_canceled_goes_to_dunning(self, db, due_subscription):
        client = FakeYooKassaClient(status="canceled")
        invoice = charge_subscription(
            subscription=due_subscription, today=date(2026, 7, 19), client=client,
        )
        assert invoice is not None
        due_subscription.refresh_from_db()
        assert due_subscription.failed_attempts == 1
        assert due_subscription.next_retry_at is not None
        payment = invoice.payments.get()
        assert payment.status == BillingPayment.Status.FAILED

    def test_provider_exception_goes_to_dunning(self, db, due_subscription):
        result = charge_subscription(
            subscription=due_subscription, today=date(2026, 7, 19),
            client=RaisingYooKassaClient(),
        )
        assert result is None
        due_subscription.refresh_from_db()
        assert due_subscription.failed_attempts == 1


class TestDunning:
    def test_retry_schedule_then_past_due(self, db, due_subscription):
        invoice = BillingInvoice.objects.create(
            subscription=due_subscription,
            period_start=date(2026, 7, 11), period_end=date(2026, 8, 10),
            subscription_amount=Decimal("690.00"), total_amount=Decimal("690.00"),
            idempotency_key="charge:test-dunning:2026-07-11",
        )
        with patch("billing.events.emit_subscription_past_due") as emit:
            # fail 1 → retry T+1d
            register_charge_failure(
                subscription=due_subscription, invoice=invoice, reason="x",
            )
            due_subscription.refresh_from_db()
            assert due_subscription.failed_attempts == 1
            assert due_subscription.status == SpecialistSubscription.Status.ACTIVE
            eta1 = due_subscription.next_retry_at
            assert eta1 > timezone.now() + timedelta(hours=23)

            # fail 2 → retry T+3d
            register_charge_failure(
                subscription=due_subscription, invoice=invoice, reason="x",
            )
            due_subscription.refresh_from_db()
            assert due_subscription.failed_attempts == 2
            assert due_subscription.next_retry_at > timezone.now() + timedelta(days=2)

            # fail 3 → past_due + C4 event with debt amount
            register_charge_failure(
                subscription=due_subscription, invoice=invoice, reason="x",
            )
            due_subscription.refresh_from_db()
            invoice.refresh_from_db()
            assert due_subscription.status == SpecialistSubscription.Status.PAST_DUE
            assert due_subscription.next_retry_at is None
            assert invoice.status == BillingInvoice.Status.FAILED
        emit.assert_called_once()
        _, kwargs = emit.call_args
        assert kwargs["failed_attempts"] == 3
        assert kwargs["debt_amount"] == Decimal("690.00")

    def test_retry_success_settles_and_resets(self, db, due_subscription):
        invoice = BillingInvoice.objects.create(
            subscription=due_subscription,
            period_start=date(2026, 7, 11), period_end=date(2026, 8, 10),
            subscription_amount=Decimal("690.00"), total_amount=Decimal("690.00"),
            idempotency_key="charge:test-retry:2026-07-11",
        )
        register_charge_failure(subscription=due_subscription, invoice=invoice)
        assert retry_open_invoice(
            subscription=due_subscription,
            client=FakeYooKassaClient(status="succeeded"),
        ) is True
        due_subscription.refresh_from_db()
        invoice.refresh_from_db()
        assert due_subscription.failed_attempts == 0
        assert due_subscription.next_retry_at is None
        assert invoice.status == BillingInvoice.Status.PAID

    def test_retry_without_open_invoice_clears_schedule(self, db, due_subscription):
        due_subscription.next_retry_at = timezone.now()
        due_subscription.save(update_fields=["next_retry_at"])
        assert retry_open_invoice(
            subscription=due_subscription, client=FakeYooKassaClient(),
        ) is False
        due_subscription.refresh_from_db()
        assert due_subscription.next_retry_at is None


class TestPayDebt:
    def _make_debt(self, subscription, *, with_card=True):
        subscription.status = SpecialistSubscription.Status.PAST_DUE
        subscription.payment_method_id = "pm_test_123" if with_card else ""
        subscription.save(update_fields=["status", "payment_method_id"])
        return BillingInvoice.objects.create(
            subscription=subscription,
            period_start=date(2026, 7, 11), period_end=date(2026, 8, 10),
            subscription_amount=Decimal("690.00"),
            fees_amount=Decimal("0.00"),
            total_amount=Decimal("690.00"),
            status=BillingInvoice.Status.FAILED,
            idempotency_key=f"charge:debt-test:{subscription.id}",
        )

    def test_no_debt_raises(self, db, subscription):
        from billing.charges import NoDebtError, pay_debt

        with pytest.raises(NoDebtError):
            pay_debt(subscription=subscription, client=FakeYooKassaClient())

    def test_instant_charge_via_saved_method(
        self, db, due_subscription, appointment,
    ):
        from billing.charges import pay_debt

        invoice = self._make_debt(due_subscription)
        BookingFee.objects.create(
            appointment=appointment, subscription=due_subscription,
            amount=Decimal("90.00"), period_start=date(2026, 7, 1),
        )
        client = FakeYooKassaClient(status="succeeded")
        result = pay_debt(subscription=due_subscription, client=client)

        # Charge = invoice (690) + pending fee accrued after it (90).
        assert result.amount == Decimal("780.00")
        assert result.confirmation_url == ""
        assert result.status == BillingPayment.Status.SUCCEEDED
        call = client.recurrent_calls[0]
        assert call["payment_method_id"] == "pm_test_123"
        assert call["amount"] == Decimal("780.00")
        assert call["metadata"]["kind"] == "debt"

        due_subscription.refresh_from_db()
        invoice.refresh_from_db()
        fee = BookingFee.objects.get(appointment=appointment)
        assert due_subscription.status == SpecialistSubscription.Status.ACTIVE
        assert invoice.status == BillingInvoice.Status.PAID
        assert invoice.fees_amount == Decimal("90.00")
        assert fee.status == BookingFee.Status.CHARGED
        assert fee.invoice_id == invoice.id
        # Period advances to the settled invoice period.
        assert due_subscription.current_period_start == date(2026, 7, 11)
        assert due_subscription.current_period_end == date(2026, 8, 10)

    def test_inflight_replay_returns_same_payment(self, db, due_subscription):
        from billing.charges import pay_debt

        self._make_debt(due_subscription)
        client = FakeYooKassaClient(status="pending")
        first = pay_debt(subscription=due_subscription, client=client)
        second = pay_debt(subscription=due_subscription, client=client)
        assert first.payment_id == second.payment_id
        assert len(client.recurrent_calls) == 1
        assert BillingPayment.objects.count() == 1

    def test_redirect_path_when_no_saved_card(self, db, due_subscription):
        from billing.charges import pay_debt

        self._make_debt(due_subscription, with_card=False)
        client = FakeYooKassaClient()
        result = pay_debt(
            subscription=due_subscription,
            return_url="https://miniapp.example/debt",
            client=client,
        )
        assert result.confirmation_url == "https://pay.example/confirm"
        assert result.status == BillingPayment.Status.PENDING
        assert len(client.setup_calls) == 1
        assert client.setup_calls[0]["return_url"] == "https://miniapp.example/debt"
        assert client.setup_calls[0]["metadata"]["kind"] == "debt"

    def test_provider_canceled_keeps_past_due(self, db, due_subscription):
        from billing.charges import pay_debt

        self._make_debt(due_subscription)
        result = pay_debt(
            subscription=due_subscription,
            client=FakeYooKassaClient(status="canceled"),
        )
        due_subscription.refresh_from_db()
        assert result.status == BillingPayment.Status.FAILED
        assert due_subscription.status == SpecialistSubscription.Status.PAST_DUE
        assert due_subscription.failed_attempts == 1
        assert due_subscription.next_retry_at is not None


class TestYooKassaClientPayload:
    """Real client, SDK-level double — verifies the wire payload shape
    (save_payment_method / payment_method_id / receipt forwarding)."""

    def _client(self):
        from billing.yookassa import BillingYooKassaClient

        calls = []

        class FakePayment:
            @staticmethod
            def create(payload, idempotency_key):
                calls.append((payload, idempotency_key))
                return SimpleNamespace(
                    id="yk_1", status="pending",
                    confirmation=SimpleNamespace(confirmation_url="https://c"),
                )

        client = BillingYooKassaClient.__new__(BillingYooKassaClient)
        client._payment_cls = FakePayment
        return client, calls

    def test_setup_payload_saves_payment_method(self):
        client, calls = self._client()
        result = client.create_setup_payment(
            amount=Decimal("690.00"), description="d",
            return_url="https://x.example", idempotency_key="k1",
            receipt={"customer": {"phone": "+7999"}}, metadata={"kind": "setup"},
        )
        payload, key = calls[0]
        assert payload["save_payment_method"] is True
        assert payload["capture"] is True
        assert payload["amount"] == {"value": "690.00", "currency": "RUB"}
        assert payload["confirmation"]["return_url"] == "https://x.example"
        assert payload["receipt"] == {"customer": {"phone": "+7999"}}
        assert key == "k1"
        assert result["confirmation_url"] == "https://c"

    def test_recurrent_payload_uses_saved_method(self):
        client, calls = self._client()
        client.create_recurrent_payment(
            amount=Decimal("690.00"), payment_method_id="pm_1",
            description="d", idempotency_key="k2",
            receipt=None, metadata={},
        )
        payload, key = calls[0]
        assert payload["payment_method_id"] == "pm_1"
        assert payload["capture"] is True
        assert "confirmation" not in payload
        assert "receipt" not in payload
        assert key == "k2"
