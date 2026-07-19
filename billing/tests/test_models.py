"""Wave-1 model invariants for the billing app (PILOT_CONTRACTS v1.3.0)."""
from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from billing.models import (
    BOOKING_FEE_AMOUNT,
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    BookingFee,
    SpecialistSubscription,
    compute_booking_fee,
    quantize_money,
)


class TestTariffPlanSeed:
    """AYLA-DEC-0001 price list, seeded by data migration."""

    def test_solo_and_salon_seeded(self, db, tariff_solo, tariff_salon):
        assert tariff_solo.price == Decimal("690.00")
        assert tariff_solo.max_masters == 1
        assert tariff_salon.price == Decimal("990.00")
        assert tariff_salon.max_masters == 3
        assert tariff_solo.is_active and tariff_salon.is_active


class TestComputeBookingFee:
    """AMD-004 edge rule: fee = min(90.00, price), never negative."""

    @pytest.mark.parametrize(
        "price,expected",
        [
            (Decimal("2000.00"), Decimal("90.00")),
            (Decimal("90.00"), Decimal("90.00")),
            (Decimal("90.01"), Decimal("90.00")),
            (Decimal("50.00"), Decimal("50.00")),
            (Decimal("0.00"), Decimal("0.00")),
            (Decimal("-10.00"), Decimal("0.00")),
        ],
    )
    def test_min_rule(self, price, expected):
        assert compute_booking_fee(price) == expected

    def test_result_is_money_shaped(self):
        # §1: 2 decimal places, ROUND_HALF_UP, Decimal.
        fee = compute_booking_fee(Decimal("89.999"))
        assert fee == Decimal("90.00")
        assert fee.as_tuple().exponent == -2

    def test_flat_fee_constant(self):
        assert BOOKING_FEE_AMOUNT == Decimal("90.00")

    def test_quantize_money_half_up(self):
        assert quantize_money(Decimal("1.005")) == Decimal("1.01")
        assert quantize_money(Decimal("1.004")) == Decimal("1.00")


class TestSubscriptionOwnership:
    def test_one_personal_subscription_per_user(self, db, subscription, tariff_solo):
        with pytest.raises(IntegrityError), transaction.atomic():
            SpecialistSubscription.objects.create(
                user=subscription.user, tariff=tariff_solo,
            )

    def test_one_subscription_per_tenant(self, db, specialist_user, tenant, tariff_salon):
        SpecialistSubscription.objects.create(
            user=specialist_user, tenant=tenant, tariff=tariff_salon,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SpecialistSubscription.objects.create(
                user=specialist_user, tenant=tenant, tariff=tariff_salon,
            )

    def test_personal_plus_salon_allowed(self, db, subscription, tenant, tariff_salon):
        # Salon owner may also hold a personal subscription.
        salon = SpecialistSubscription.objects.create(
            user=subscription.user, tenant=tenant, tariff=tariff_salon,
        )
        assert salon.tenant_id == tenant.id
        assert SpecialistSubscription.objects.filter(
            user=subscription.user,
        ).count() == 2

    def test_default_status_is_trial(self, db, specialist_user, tariff_solo):
        sub = SpecialistSubscription.objects.create(
            user=specialist_user, tariff=tariff_solo,
        )
        assert sub.status == SpecialistSubscription.Status.TRIAL
        assert sub.payment_method_id == ""
        assert sub.failed_attempts == 0


class TestBookingFeeInvariant:
    """C4 / AYLA-DEC-0010: one appointment → at most one BookingFee."""

    def _create_fee(self, appointment, subscription, **overrides):
        defaults = dict(
            appointment=appointment,
            subscription=subscription,
            amount=compute_booking_fee(appointment.price),
            period_start=date(2026, 7, 1),
        )
        defaults.update(overrides)
        return BookingFee.objects.create(**defaults)

    def test_unique_per_appointment(self, db, appointment, subscription):
        self._create_fee(appointment, subscription)
        with pytest.raises(IntegrityError), transaction.atomic():
            self._create_fee(appointment, subscription)

    def test_defaults(self, db, appointment, subscription):
        fee = self._create_fee(appointment, subscription)
        assert fee.status == BookingFee.Status.PENDING
        assert fee.invoice is None
        assert fee.amount == Decimal("90.00")  # price 1500.00 → flat 90

    def test_fee_capped_by_cheap_service(self, db, appointment, subscription):
        appointment.price = Decimal("50.00")
        fee = self._create_fee(
            appointment, subscription,
            amount=compute_booking_fee(appointment.price),
        )
        assert fee.amount == Decimal("50.00")


class TestBillingInvoice:
    def test_idempotency_key_unique(self, db, subscription):
        BillingInvoice.objects.create(
            subscription=subscription,
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            subscription_amount=Decimal("690.00"),
            fees_amount=Decimal("270.00"),
            total_amount=Decimal("960.00"),
            idempotency_key="charge:sub-1:2026-07",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            BillingInvoice.objects.create(
                subscription=subscription,
                period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
                idempotency_key="charge:sub-1:2026-07",
            )


class TestBillingPayment:
    def test_idempotency_key_unique(self, db, subscription):
        invoice = BillingInvoice.objects.create(
            subscription=subscription,
            period_start=date(2026, 7, 1), period_end=date(2026, 7, 31),
            total_amount=Decimal("690.00"),
            idempotency_key="charge:sub-1:2026-07",
        )
        BillingPayment.objects.create(
            invoice=invoice, kind=BillingPayment.Kind.RECURRENT,
            amount=Decimal("690.00"), idempotency_key="pay:inv-1:attempt-1",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            BillingPayment.objects.create(
                invoice=invoice, kind=BillingPayment.Kind.RECURRENT,
                amount=Decimal("690.00"), idempotency_key="pay:inv-1:attempt-1",
            )


class TestBillingConsent:
    def test_single_active_consent_per_user(self, db, specialist_user):
        BillingConsent.objects.create(
            user=specialist_user, document_version="offer-0.9",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            BillingConsent.objects.create(
                user=specialist_user, document_version="offer-1.0",
            )

    def test_new_consent_allowed_after_revoke(self, db, specialist_user):
        from django.utils import timezone

        consent = BillingConsent.objects.create(
            user=specialist_user, document_version="offer-0.9",
        )
        consent.revoked_at = timezone.now()
        consent.save(update_fields=["revoked_at"])
        fresh = BillingConsent.objects.create(
            user=specialist_user, document_version="offer-1.0",
        )
        assert fresh.revoked_at is None
        assert BillingConsent.objects.filter(user=specialist_user).count() == 2
