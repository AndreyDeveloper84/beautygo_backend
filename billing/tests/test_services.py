"""C1 eligibility, AMD-009 fee accrual, C2 status payload (service layer)."""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from billing.models import BillingInvoice, BookingFee, SpecialistSubscription
from billing.services import (
    EligibilityResult,
    accrue_booking_fee,
    build_billing_status,
    can_accept_booking,
    has_online_payment,
    resolve_billing_account,
)
from payments.models import Payment


class TestEligibilityResult:
    """C1 dataclass invariants (frozen, slots — AMD-003 shape)."""

    def test_ok_requires_no_reason(self):
        assert EligibilityResult(ok=True).reason is None
        with pytest.raises(ValueError):
            EligibilityResult(ok=True, reason="SUBSCRIPTION_PAST_DUE")

    def test_block_requires_past_due_reason(self):
        result = EligibilityResult(ok=False, reason="SUBSCRIPTION_PAST_DUE")
        assert result.ok is False
        with pytest.raises(ValueError):
            EligibilityResult(ok=False)
        with pytest.raises(ValueError):
            EligibilityResult(ok=False, reason="SOMETHING_ELSE")

    def test_frozen(self):
        result = EligibilityResult(ok=True)
        with pytest.raises(AttributeError):
            result.ok = False


class TestCanAcceptBooking:
    def test_no_account_is_fail_open(self, db, specialist_user):
        with patch("billing.services.sentry_sdk.capture_message") as sentry_msg:
            result = can_accept_booking(specialist_user.id, None)
        assert result == EligibilityResult(ok=True)
        sentry_msg.assert_called_once()

    @pytest.mark.parametrize("status", [
        SpecialistSubscription.Status.TRIAL,
        SpecialistSubscription.Status.ACTIVE,
        SpecialistSubscription.Status.CANCELED,
    ])
    def test_non_past_due_statuses_allow(self, db, subscription, status):
        subscription.status = status
        subscription.save(update_fields=["status"])
        assert can_accept_booking(subscription.user_id, None).ok is True

    def test_past_due_blocks_with_reason(self, db, subscription):
        subscription.status = SpecialistSubscription.Status.PAST_DUE
        subscription.save(update_fields=["status"])
        result = can_accept_booking(subscription.user_id, None)
        assert result.ok is False
        assert result.reason == "SUBSCRIPTION_PAST_DUE"

    def test_salon_past_due_blocks_masters(self, db, salon_subscription, tenant):
        # Salon account governs ALL its masters (C1), even when the
        # master personally has no debt (here: no personal account).
        salon_subscription.status = SpecialistSubscription.Status.PAST_DUE
        salon_subscription.save(update_fields=["status"])
        result = can_accept_booking(salon_subscription.user_id, tenant.id)
        assert result.ok is False
        assert result.reason == "SUBSCRIPTION_PAST_DUE"

    def test_tenant_without_salon_account_falls_back_to_personal(
        self, db, subscription, tenant,
    ):
        # tenant has no salon subscription → personal subscription rules.
        subscription.status = SpecialistSubscription.Status.PAST_DUE
        subscription.save(update_fields=["status"])
        result = can_accept_booking(subscription.user_id, tenant.id)
        assert result.ok is False

    def test_technical_error_is_fail_open(self, db, specialist_user):
        with patch(
            "billing.services.resolve_billing_account",
            side_effect=RuntimeError("db down"),
        ), patch("billing.services.sentry_sdk.capture_exception") as sentry_exc:
            result = can_accept_booking(specialist_user.id, None)
        assert result == EligibilityResult(ok=True)
        sentry_exc.assert_called_once()


class TestResolveBillingAccount:
    def test_salon_account_wins_in_tenant_context(
        self, db, subscription, salon_subscription, tenant,
    ):
        account = resolve_billing_account(
            user_id=subscription.user_id, tenant_id=tenant.id,
        )
        assert account.pk == salon_subscription.pk

    def test_personal_account_when_no_tenant(self, db, subscription):
        account = resolve_billing_account(
            user_id=subscription.user_id, tenant_id=None,
        )
        assert account.pk == subscription.pk

    def test_salon_owner_personal_lookup_ignores_salon(
        self, db, subscription, salon_subscription,
    ):
        account = resolve_billing_account(
            user_id=subscription.user_id, tenant_id=None,
        )
        assert account.tenant_id is None


class TestHasOnlinePayment:
    @pytest.mark.parametrize("status", [
        Payment.Status.AUTHORIZED,
        Payment.Status.PAID,
        Payment.Status.REFUNDED,            # AMD-009: paid+refund — no fee
        Payment.Status.PARTIALLY_REFUNDED,
    ])
    def test_online_states(self, db, appointment, status):
        Payment.objects.create(
            appointment=appointment, amount=Decimal("1500.00"), status=status,
        )
        assert has_online_payment(appointment) is True

    @pytest.mark.parametrize("status", [
        Payment.Status.PENDING,
        Payment.Status.FAILED,
    ])
    def test_abandoned_states_are_not_online(self, db, appointment, status):
        Payment.objects.create(
            appointment=appointment, amount=Decimal("1500.00"), status=status,
        )
        assert has_online_payment(appointment) is False

    def test_no_payments(self, db, appointment):
        assert has_online_payment(appointment) is False


class TestAccrueBookingFee:
    def test_accrues_once_and_emits_event(self, db, appointment, subscription):
        with patch("billing.events.emit_fee_charged") as emit:
            fee = accrue_booking_fee(appointment)
            again = accrue_booking_fee(appointment)  # retry / duplicate event

        assert fee is not None
        assert again.pk == fee.pk
        # C4 invariant: exactly one fee row per appointment.
        assert BookingFee.objects.filter(appointment=appointment).count() == 1
        assert fee.amount == Decimal("90.00")
        assert fee.subscription_id == subscription.id
        assert fee.status == BookingFee.Status.PENDING
        assert fee.period_start == timezone.localtime(
            appointment.end_datetime,
        ).date().replace(day=1)
        # ...and exactly one event emission (retry does not re-emit).
        emit.assert_called_once_with(fee)

    def test_online_paid_accrues_nothing(self, db, appointment, subscription):
        Payment.objects.create(
            appointment=appointment, amount=Decimal("1500.00"),
            status=Payment.Status.PAID,
        )
        assert accrue_booking_fee(appointment) is None
        assert BookingFee.objects.count() == 0

    def test_failed_payment_still_accrues(self, db, appointment, subscription):
        Payment.objects.create(
            appointment=appointment, amount=Decimal("1500.00"),
            status=Payment.Status.FAILED,
        )
        with patch("billing.events.emit_fee_charged"):
            assert accrue_booking_fee(appointment) is not None

    def test_no_account_is_reconciliation_incident(self, db, appointment):
        # AYLA-DEC-0010: a completed booking with NO charge anywhere is
        # an incident — alert, don't crash.
        with patch("billing.services.sentry_sdk.capture_message") as sentry_msg:
            assert accrue_booking_fee(appointment) is None
        assert BookingFee.objects.count() == 0
        sentry_msg.assert_called_once()

    def test_salon_fee_lands_on_salon_account(
        self, db, appointment, salon_subscription, tenant, specialist,
    ):
        specialist.tenant = tenant
        specialist.save(update_fields=["tenant"])
        appointment.tenant = tenant
        appointment.save(update_fields=["tenant"])
        with patch("billing.events.emit_fee_charged"):
            fee = accrue_booking_fee(appointment)
        assert fee.subscription_id == salon_subscription.id

    def test_emit_failure_keeps_fee(self, db, appointment, subscription):
        # Topics unregistered pre-W1-patch → emit raises; fee survives.
        with patch(
            "billing.events.emit_fee_charged", side_effect=ValueError("topic"),
        ), patch("billing.services.sentry_sdk.capture_exception"):
            fee = accrue_booking_fee(appointment)
        assert fee is not None
        assert BookingFee.objects.filter(appointment=appointment).count() == 1

    def test_fee_capped_by_cheap_service(self, db, appointment, subscription):
        appointment.price = Decimal("50.00")
        appointment.save(update_fields=["price"])
        with patch("billing.events.emit_fee_charged"):
            fee = accrue_booking_fee(appointment)
        assert fee.amount == Decimal("50.00")


class TestBuildBillingStatus:
    def test_none_shape_without_account(self, db, specialist):
        assert build_billing_status(specialist) == {
            "specialist_id": str(specialist.user_id),
            "subscription": {
                "status": "none",
                "tariff": None,
                "current_period_end": None,
                "next_charge": None,
            },
            "fees": {"pending_total": "0.00", "pending_count": 0},
            "last_invoice": None,
        }

    def test_active_shape_with_fees_and_invoice(
        self, db, specialist, subscription, service,
    ):
        for _ in range(3):
            appt = _make_appointment(specialist, service)
            BookingFee.objects.create(
                appointment=appt, subscription=subscription,
                amount=Decimal("90.00"), period_start=date(2026, 7, 1),
            )
        invoice = BillingInvoice.objects.create(
            subscription=subscription,
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            subscription_amount=Decimal("690.00"),
            fees_amount=Decimal("180.00"),
            total_amount=Decimal("870.00"),
            status=BillingInvoice.Status.PAID,
            idempotency_key="charge:test:2026-06",
            paid_at=timezone.now(),
        )
        payload = build_billing_status(specialist)
        assert payload["specialist_id"] == str(specialist.user_id)
        sub = payload["subscription"]
        assert sub["status"] == "active"
        assert sub["tariff"] == "solo"
        assert sub["current_period_end"] == "2026-07-31"
        assert sub["next_charge"] == {
            "subscription_amount": "690.00",
            "fees_amount": "270.00",
            "total_amount": "960.00",
            "date": "2026-08-01",
        }
        assert payload["fees"] == {"pending_total": "270.00", "pending_count": 3}
        assert payload["last_invoice"]["id"] == str(invoice.id)
        assert payload["last_invoice"]["amount"] == "870.00"
        assert payload["last_invoice"]["status"] == "paid"
        assert payload["last_invoice"]["paid_at"] is not None

    def test_canceled_has_no_next_charge(self, db, specialist, subscription):
        subscription.status = SpecialistSubscription.Status.CANCELED
        subscription.save(update_fields=["status"])
        payload = build_billing_status(specialist)
        assert payload["subscription"]["status"] == "canceled"
        assert payload["subscription"]["next_charge"] is None

    def test_salon_account_reported_for_salon_master(
        self, db, specialist, salon_subscription, tenant,
    ):
        specialist.tenant = tenant
        specialist.save(update_fields=["tenant"])
        payload = build_billing_status(specialist)
        assert payload["subscription"]["status"] == "active"
        assert payload["subscription"]["tariff"] == "salon"
        assert payload["subscription"]["next_charge"]["subscription_amount"] == "990.00"


def _make_appointment(specialist, service):
    """Minimal extra appointment for fee aggregation tests."""
    from appointments.models import Appointment

    start = timezone.now() + timezone.timedelta(hours=3)
    return Appointment.objects.create(
        client=_client(),
        specialist=specialist,
        service=service,
        start_datetime=start,
        end_datetime=start + timezone.timedelta(minutes=60),
        price=service.price,
        status=Appointment.Status.CONFIRMED,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )


def _client():
    from users.models import User

    user, _ = User.objects.get_or_create(
        username="billing_status_client",
        defaults={"role": "client", "phone": "+79990000030"},
    )
    return user
