"""DRF-2984 PR-C2 — tariffs of 20.10: extra master price, trial month,
soft freeze, catalog-counted masters, events inside the transaction.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from billing import tasks
from billing.charges import (
    charge_subscription,
    freeze_ended_trials,
    register_charge_failure,
    settle_charge_success,
    start_trial,
)
from billing.models import (
    BillingInvoice,
    BillingPayment,
    SpecialistSubscription,
    TariffPlan,
)
from billing.pricing import billable_masters_count, period_price
from billing.services import REASON_SUBSCRIPTION_PAST_DUE, can_accept_booking
from tenants.models import Tenant
from users.models import SpecialistProfile, User

_phone_seq = iter(range(100, 999))


def _master(tenant, *, status="active", available=True, accepting=True):
    n = next(_phone_seq)
    user = User.objects.create_user(
        username=f"c2_master_{n}", password="x", role="specialist",
        phone=f"+7999300{n:04d}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.status = status
    profile.is_available = available
    profile.is_booking_enabled = accepting
    profile.save()
    return profile


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name="C2 Salon", slug="c2-salon")


@pytest.fixture
def salon_sub(db, specialist_user, salon, tariff_salon):
    return SpecialistSubscription.objects.create(
        user=specialist_user, tenant=salon, tariff=tariff_salon,
        status=SpecialistSubscription.Status.ACTIVE,
        current_period_start=date(2026, 6, 11),
        current_period_end=date(2026, 7, 10),
        payment_method_id="pm_c2",
    )


class TestSeededTariffs:
    def test_salon_has_extra_price_solo_has_none(self, tariff_salon, tariff_solo):
        assert tariff_salon.price == Decimal("990.00")
        assert tariff_salon.max_masters == 3
        assert tariff_salon.extra_master_price == Decimal("150.00")
        assert tariff_solo.extra_master_price is None
        assert tariff_salon.trial_months == tariff_solo.trial_months == 1

    def test_seed_creates_no_rows(self, db):
        assert TariffPlan.objects.count() == 2


class TestBillableMasters:
    def test_counts_only_masters_on_sale(self, salon, salon_sub):
        _master(salon)
        _master(salon)
        _master(salon, accepting=False)      # paused (also: on leave, В-3)
        _master(salon, available=False)      # out of the pool (archived)
        _master(salon, status="draft")       # not published
        _master(Tenant.objects.create(name="Other", slug="c2-other"))
        assert billable_masters_count(salon_sub) == 2

    def test_personal_account_is_one_master(self, subscription):
        assert billable_masters_count(subscription) == 1


class TestPeriodPrice:
    @pytest.mark.parametrize("masters,extra,total", [
        (1, "0.00", "990.00"),
        (3, "0.00", "990.00"),
        (4, "150.00", "1140.00"),
        (6, "450.00", "1440.00"),
    ])
    def test_salon(self, salon_sub, masters, extra, total):
        price = period_price(salon_sub, masters_count=masters)
        assert price.masters_count == masters
        assert price.extra_masters_amount == Decimal(extra)
        assert price.total == Decimal(total)

    def test_overrides_replace_plan_prices(self, salon_sub):
        salon_sub.price_override = Decimal("500.00")
        salon_sub.extra_master_price_override = Decimal("100.00")
        price = period_price(salon_sub, masters_count=5)
        assert price.base_amount == Decimal("500.00")
        assert price.extra_masters_amount == Decimal("200.00")
        assert price.total == Decimal("700.00")

    def test_solo_has_no_extra_line(self, subscription):
        price = period_price(subscription, masters_count=3)
        assert price.extra_masters_amount == Decimal("0.00")
        assert price.total == Decimal("690.00")

    def test_invoice_carries_the_snapshot(self, salon, salon_sub):
        for _ in range(4):
            _master(salon)
        with patch("billing.charges.BillingYooKassaClient") as client_cls:
            client_cls.return_value.create_recurrent_payment.return_value = {
                "provider_payment_id": "yk_c2", "status": "pending",
            }
            invoice = charge_subscription(
                subscription=salon_sub, today=date(2026, 7, 11),
            )
        assert invoice.masters_count == 4
        assert invoice.extra_masters_amount == Decimal("150.00")
        assert invoice.subscription_amount == Decimal("1140.00")
        assert invoice.total_amount == Decimal("1140.00")


class TestTrial:
    def test_date_to_date(self, salon_sub):
        salon_sub.trial_ends_at = None
        start_trial(salon_sub, started_on=date(2026, 10, 20))
        salon_sub.refresh_from_db()
        assert salon_sub.status == SpecialistSubscription.Status.TRIAL
        assert salon_sub.trial_ends_at == date(2026, 11, 20)
        assert salon_sub.current_period_start == date(2026, 10, 20)
        assert salon_sub.current_period_end == date(2026, 11, 19)

    def test_month_end_clamps(self, salon_sub):
        start_trial(salon_sub, started_on=date(2027, 1, 31))
        assert salon_sub.trial_ends_at == date(2027, 2, 28)

    def test_idempotent(self, salon_sub):
        start_trial(salon_sub, started_on=date(2026, 10, 20))
        start_trial(salon_sub, started_on=date(2026, 12, 1))
        salon_sub.refresh_from_db()
        assert salon_sub.trial_ends_at == date(2026, 11, 20)


class TestFreezeEndedTrials:
    @pytest.fixture
    def trial(self, salon_sub):
        salon_sub.payment_method_id = ""
        salon_sub.save(update_fields=["payment_method_id"])
        return start_trial(salon_sub, started_on=date(2026, 9, 1))

    def test_ended_without_card_freezes(self, trial):
        assert freeze_ended_trials(today=date(2026, 10, 1)) == 1
        trial.refresh_from_db()
        assert trial.status == SpecialistSubscription.Status.FROZEN
        assert trial.frozen_reason == "trial_ended_no_card"
        assert trial.frozen_at is not None

    def test_not_yet_ended(self, trial):
        assert freeze_ended_trials(today=date(2026, 9, 30)) == 0

    def test_with_card_not_frozen(self, trial):
        trial.payment_method_id = "pm_x"
        trial.save(update_fields=["payment_method_id"])
        assert freeze_ended_trials(today=date(2026, 10, 1)) == 0

    def test_legacy_trial_without_end_untouched(self, subscription):
        subscription.status = SpecialistSubscription.Status.TRIAL
        subscription.save(update_fields=["status"])
        assert freeze_ended_trials(today=date(2030, 1, 1)) == 0

    @pytest.mark.parametrize("enabled,charges,frozen", [
        (False, False, False),
        (True, False, False),   # no card can be saved → do not freeze
        (True, True, True),
    ])
    def test_beat_task_freezes_only_with_both_switches(
        self, settings, trial, enabled, charges, frozen,
    ):
        settings.BILLING_ENABLED = enabled
        settings.BILLING_CHARGES_ENABLED = charges
        with patch("billing.tasks.timezone.localdate", return_value=date(2026, 10, 1)):
            tasks.charge_subscriptions_monthly()
        trial.refresh_from_db()
        assert (trial.status == SpecialistSubscription.Status.FROZEN) is frozen


class TestSoftFreeze:
    def test_frozen_blocks_new_bookings(self, specialist, salon_sub):
        salon_sub.status = SpecialistSubscription.Status.FROZEN
        salon_sub.save(update_fields=["status"])
        result = can_accept_booking(specialist.user_id, salon_sub.tenant_id)
        assert result.ok is False
        assert result.reason == REASON_SUBSCRIPTION_PAST_DUE

    def test_dunning_keeps_bookings_open_until_exhausted(
        self, specialist, salon_sub,
    ):
        invoice = BillingInvoice.objects.create(
            subscription=salon_sub, period_start=date(2026, 7, 11),
            period_end=date(2026, 8, 10), total_amount=Decimal("990.00"),
            idempotency_key="charge:c2-dunning",
        )
        for _ in range(2):
            register_charge_failure(subscription=salon_sub, invoice=invoice)
            salon_sub.refresh_from_db()
            assert salon_sub.status == SpecialistSubscription.Status.ACTIVE
            assert can_accept_booking(specialist.user_id, salon_sub.tenant_id).ok
        register_charge_failure(subscription=salon_sub, invoice=invoice)
        salon_sub.refresh_from_db()
        assert salon_sub.status == SpecialistSubscription.Status.FROZEN
        assert not can_accept_booking(specialist.user_id, salon_sub.tenant_id).ok

    def test_paying_lifts_the_freeze(self, salon_sub):
        salon_sub.status = SpecialistSubscription.Status.FROZEN
        salon_sub.frozen_at = timezone.now()
        salon_sub.frozen_reason = "past_due_timeout"
        salon_sub.save()
        invoice = BillingInvoice.objects.create(
            subscription=salon_sub, period_start=date(2026, 7, 11),
            period_end=date(2026, 8, 10), total_amount=Decimal("990.00"),
            idempotency_key="charge:c2-lift",
        )
        payment = BillingPayment.objects.create(
            invoice=invoice, kind=BillingPayment.Kind.RECURRENT,
            amount=Decimal("990.00"), idempotency_key="pay:c2-lift",
            provider_payment_id="yk_c2_lift",
        )
        settle_charge_success(
            subscription=salon_sub, invoice=invoice, payment_row=payment,
        )
        salon_sub.refresh_from_db()
        assert salon_sub.status == SpecialistSubscription.Status.ACTIVE
        assert salon_sub.frozen_at is None
        assert salon_sub.frozen_reason == ""


class TestEventsInsideTransaction:
    def test_failed_emit_rolls_the_freeze_back(self, salon_sub):
        invoice = BillingInvoice.objects.create(
            subscription=salon_sub, period_start=date(2026, 7, 11),
            period_end=date(2026, 8, 10), total_amount=Decimal("990.00"),
            idempotency_key="charge:c2-atomic",
        )
        salon_sub.failed_attempts = 2
        salon_sub.save(update_fields=["failed_attempts"])
        with patch(
            "billing.events.emit_outbox_event", side_effect=RuntimeError("outbox"),
        ):
            with pytest.raises(RuntimeError):
                register_charge_failure(subscription=salon_sub, invoice=invoice)
        salon_sub.refresh_from_db()
        invoice.refresh_from_db()
        assert salon_sub.status == SpecialistSubscription.Status.ACTIVE
        assert salon_sub.failed_attempts == 2
        assert invoice.status == BillingInvoice.Status.OPEN

    def test_failed_emit_rolls_the_activation_back(self, salon_sub):
        salon_sub.status = SpecialistSubscription.Status.TRIAL
        salon_sub.save(update_fields=["status"])
        invoice = BillingInvoice.objects.create(
            subscription=salon_sub, period_start=date(2026, 7, 11),
            period_end=date(2026, 8, 10), total_amount=Decimal("990.00"),
            idempotency_key="charge:c2-atomic-act",
        )
        payment = BillingPayment.objects.create(
            invoice=invoice, kind=BillingPayment.Kind.RECURRENT,
            amount=Decimal("990.00"), idempotency_key="pay:c2-atomic-act",
            provider_payment_id="yk_c2_act",
        )
        with patch(
            "billing.events.emit_outbox_event", side_effect=RuntimeError("outbox"),
        ):
            with pytest.raises(RuntimeError):
                settle_charge_success(
                    subscription=salon_sub, invoice=invoice, payment_row=payment,
                )
        salon_sub.refresh_from_db()
        payment.refresh_from_db()
        assert salon_sub.status == SpecialistSubscription.Status.TRIAL
        assert payment.status == BillingPayment.Status.PENDING

    def test_events_carry_tenant_id(self, salon_sub):
        invoice = BillingInvoice.objects.create(
            subscription=salon_sub, period_start=date(2026, 7, 11),
            period_end=date(2026, 8, 10), total_amount=Decimal("990.00"),
            idempotency_key="charge:c2-tenant",
        )
        salon_sub.failed_attempts = 2
        salon_sub.save(update_fields=["failed_attempts"])
        with patch("billing.events.emit_outbox_event") as emit:
            register_charge_failure(subscription=salon_sub, invoice=invoice)
        assert emit.call_args.kwargs["data"]["tenant_id"] == str(salon_sub.tenant_id)
