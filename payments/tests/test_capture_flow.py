"""Phase B (pilot, D1/D6/D8/D9) — payments vertical slice.

Pins:
- flat 90₽ platform fee (AYLA-DEC-0001, replaces 8%) in YooKassa payload
  math and booking snapshots;
- split per-master (AYLA-DEC-0008): transfers target the specialist's own
  sub-account; no sub-account ⇒ ONLINE_PAYMENT_UNAVAILABLE while the
  no-prepayment booking path keeps working;
- capture on complete() (AYLA-DEC-0009): deferred task, stable
  idempotency key, expires_at clamp, state transitions;
- booking cancel ⇒ hold auto-cancelled (acceptance #5);
- AYLA-DEC-0010 (W1 side): an online-paid booking carries exactly the
  90₽ platform fee — BookingFee is W2's side, covered by the joint
  invariant test in Phase C.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.application.dto import CancelBookingDTO
from appointments.application.services.cancel_reschedule_service import (
    CancelBookingService,
)
from appointments.models import Appointment
from payments.exceptions import SpecialistPayoutNotConfiguredError
from payments.models import Payment
from payments.services import (
    YooKassaService,
    compute_capture_at,
    get_platform_fee,
)
from payments.tasks import capture_payment_task
from services.models import Service, ServiceCategory
from users.models import User

CREATE_URL = '/api/v1/payments/create/'
WEBHOOK_URL = '/api/v1/payments/webhook/'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Ногти B')


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username='cap_specialist', password='pass',
        role='specialist', phone='+79990700020',
    )
    p = user.specialist_profile
    p.display_name = 'Мастер B'
    p.status = 'active'
    p.is_available = True
    p.is_booking_enabled = True
    p.yookassa_account_id = 'yk-subacc-master-1'
    p.save()
    return user


@pytest.fixture
def service(db, specialist_user, category):
    return Service.objects.create(
        specialist=specialist_user.specialist_profile,
        category=category,
        name='Маникюр B',
        price=Decimal('2000.00'),
        duration_minutes=60,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='cap_client', password='pass',
        role='client', phone='+79990700021',
    )


def _make_appointment(client_user, specialist_user, service, appt_status):
    now = timezone.now()
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist_user.specialist_profile,
        service=service,
        start_datetime=now + timedelta(hours=2),
        end_datetime=now + timedelta(hours=3),
        status=appt_status,
        price=service.price,
    )


def _held_payment(appt) -> Payment:
    """A payment in the post-hold state (webhook waiting_for_capture done)."""
    return Payment.objects.create(
        appointment=appt,
        amount=appt.price,
        status=Payment.Status.AUTHORIZED,
        specialist_income=appt.price - Decimal('90.00'),
        platform_fee=Decimal('90.00'),
        provider='yookassa',
        provider_payment_id='yk_hold_001',
        capture_state=Payment.CaptureState.SCHEDULED,
        yookassa_expires_at=timezone.now() + timedelta(days=7),
    )


def _svc_with_mocked_sdk(capture_status='succeeded'):
    """YooKassaService instance with a mocked SDK class (no HTTP, no creds)."""
    svc = YooKassaService.__new__(YooKassaService)
    sdk_payment = MagicMock()
    sdk_payment.capture.return_value = MagicMock(id='yk_hold_001', status=capture_status)
    sdk_payment.cancel.return_value = MagicMock(id='yk_hold_001', status='canceled')
    svc._payment_cls = sdk_payment
    return svc, sdk_payment


# ---------------------------------------------------------------------------
# Flat 90₽ fee (D1)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestFlatPlatformFee:
    def test_fee_is_flat_90(self):
        assert get_platform_fee(Decimal('2000.00')) == Decimal('90.00')
        assert get_platform_fee(Decimal('500.00')) == Decimal('90.00')

    def test_fee_capped_at_amount(self):
        """Degenerate sub-90₽ charge: non-negative income (contract §1)."""
        assert get_platform_fee(Decimal('50.00')) == Decimal('50.00')

    def test_create_payment_payload_math_and_split(self):
        """D1+D8: 2000₽ → 90₽ platform / 1910₽ transfer to the MASTER's
        own sub-account (never the deprecated shared one)."""
        svc, sdk_payment = _svc_with_mocked_sdk()
        sdk_payment.create.return_value = MagicMock(
            id='yk_new_001', status='pending', confirmation=None,
        )
        result = svc.create_payment(
            amount=Decimal('2000.00'),
            appointment_id=uuid.uuid4(),
            description='test',
            return_url='https://example.ru/ok',
            idempotency_key='idem-1',
            capture=False,
            specialist_account_id='yk-subacc-master-1',
        )
        assert result['platform_fee'] == Decimal('90.00')
        assert result['specialist_income'] == Decimal('1910.00')
        payload = sdk_payment.create.call_args.args[0]
        assert payload['transfers'] == [{
            'account_id': 'yk-subacc-master-1',
            'amount': {'value': '1910.00', 'currency': 'RUB'},
        }]
        assert payload['metadata']['platform_fee'] == '90.00'
        assert payload['capture'] is False  # two-stage hold

    def test_create_payment_without_subaccount_refused(self):
        svc, _ = _svc_with_mocked_sdk()
        with pytest.raises(SpecialistPayoutNotConfiguredError):
            svc.create_payment(
                amount=Decimal('2000.00'),
                appointment_id=uuid.uuid4(),
                description='test',
                return_url='https://example.ru/ok',
                idempotency_key='idem-2',
                specialist_account_id='',
            )


@pytest.mark.django_db
class TestOnlinePaymentUnavailable:
    def test_create_endpoint_422_without_subaccount(
        self, client_user, specialist_user, service,
    ):
        """D8: no sub-account → 422 ONLINE_PAYMENT_UNAVAILABLE…"""
        specialist_user.specialist_profile.yookassa_account_id = ''
        specialist_user.specialist_profile.save()
        appt = _make_appointment(
            client_user, specialist_user, service, 'awaiting_payment',
        )
        api = APIClient()
        api.defaults['HTTP_X_APP_TYPE'] = 'client'
        api.force_authenticate(user=client_user)
        with patch(
            'payments.views._get_yookassa',
            return_value=_svc_with_mocked_sdk()[0],
        ):
            r = api.post(CREATE_URL, {
                'appointment_id': str(appt.id),
                'return_url': 'https://example.ru/ok',
            }, format='json')
        assert r.status_code == 422, r.data
        assert r.data['error']['code'] == 'ONLINE_PAYMENT_UNAVAILABLE'

    def test_booking_without_prepayment_still_works(
        self, client_user, specialist_user, service,
    ):
        """…and the no-prepayment path (D6) is unaffected by the missing
        sub-account."""
        specialist_user.specialist_profile.yookassa_account_id = ''
        specialist_user.specialist_profile.save()
        api = APIClient()
        api.defaults['HTTP_X_APP_TYPE'] = 'client'
        api.force_authenticate(user=client_user)
        start = (timezone.now() + timedelta(hours=3)).replace(
            second=0, microsecond=0,
        )
        r = api.post('/api/v1/appointments/', {
            'specialist_id': str(specialist_user.specialist_profile.id),
            'service_id': str(service.id),
            'start_datetime': start.isoformat(),
            'payment_required': False,
        }, format='json')
        assert r.status_code == 201, r.data
        assert Appointment.objects.get().status == Appointment.Status.CONFIRMED


# ---------------------------------------------------------------------------
# Capture planning (D9, ADR)
# ---------------------------------------------------------------------------

class TestCapturePlanning:
    def test_zero_delay_captures_immediately(self, settings):
        settings.CAPTURE_DELAY_HOURS = 0
        now = timezone.now()
        assert compute_capture_at(
            completed_at=now, expires_at=now + timedelta(days=7),
        ) == now

    def test_delay_applies_when_configured(self, settings):
        settings.CAPTURE_DELAY_HOURS = 24
        now = timezone.now()
        assert compute_capture_at(
            completed_at=now, expires_at=now + timedelta(days=7),
        ) == now + timedelta(hours=24)

    def test_delay_clamped_to_expires_at_minus_buffer(self, settings):
        """A 24h delay on a 2h hold must NOT outlive expires_at − 60m."""
        settings.CAPTURE_DELAY_HOURS = 24
        settings.CAPTURE_SAFETY_BUFFER_MINUTES = 60
        now = timezone.now()
        expires = now + timedelta(hours=2)
        assert compute_capture_at(completed_at=now, expires_at=expires) == (
            expires - timedelta(minutes=60)
        )

    def test_missing_expires_at_uses_delay_only(self, settings):
        settings.CAPTURE_DELAY_HOURS = 24
        now = timezone.now()
        assert compute_capture_at(completed_at=now, expires_at=None) == (
            now + timedelta(hours=24)
        )


# ---------------------------------------------------------------------------
# Capture on complete() (D9) — eager celery in tests
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCaptureOnComplete:
    def _complete(self, specialist_user, appt):
        api = APIClient()
        api.defaults['HTTP_X_APP_TYPE'] = 'pro'
        api.force_authenticate(user=specialist_user)
        return api.post(f'/api/v1/appointments/{appt.id}/complete/')

    def test_complete_captures_held_payment(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk(capture_status='succeeded')
        with patch('payments.services.YooKassaService', return_value=svc):
            r = self._complete(specialist_user, appt)
        assert r.status_code == 200, r.data
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PAID
        assert payment.capture_state == (
            Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
        )
        assert payment.captured_at is not None
        # Stable idempotency key — the retry-safety contract.
        sdk_payment.capture.assert_called_once_with(
            'yk_hold_001',
            {'amount': {'value': '2000.00', 'currency': 'RUB'}},
            f'capture-{payment.id}',
        )

    def test_capture_task_is_idempotent_noop_after_success(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk()
        with patch('payments.services.YooKassaService', return_value=svc):
            capture_payment_task(str(payment.id))
            capture_payment_task(str(payment.id))  # duplicate delivery
        assert sdk_payment.capture.call_count == 1

    def test_complete_without_held_payment_is_noop(
        self, client_user, specialist_user, service,
    ):
        """No-prepayment booking (D6): complete() works, no capture attempted."""
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        svc, sdk_payment = _svc_with_mocked_sdk()
        with patch('payments.services.YooKassaService', return_value=svc):
            r = self._complete(specialist_user, appt)
        assert r.status_code == 200, r.data
        sdk_payment.capture.assert_not_called()

    def test_transient_error_signals_retry_without_state_change(
        self, client_user, specialist_user, service,
    ):
        """A transient provider failure leaves the payment SCHEDULED and
        signals celery to retry (Retry/PaymentClientError out of the
        eager call). Celery eager mode raises the retry signal instead
        of looping — the loop itself is worker-only behaviour."""
        from celery.exceptions import Retry
        from payments.exceptions import PaymentClientError
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk()
        sdk_payment.capture.side_effect = PaymentClientError('boom')
        with patch('payments.services.YooKassaService', return_value=svc):
            with pytest.raises((Retry, PaymentClientError)):
                capture_payment_task(str(payment.id))
        payment.refresh_from_db()
        assert payment.capture_state == Payment.CaptureState.SCHEDULED
        assert payment.status == Payment.Status.AUTHORIZED

    def test_exhausted_retries_pin_capture_failed(
        self, client_user, specialist_user, service,
    ):
        """When the retry budget is exhausted (request.retries >= max),
        the task pins capture_failed for reconciliation and returns
        WITHOUT raising — the complete() request must never 500 on a
        capture problem."""
        from payments.exceptions import PaymentClientError
        from payments.tasks import CAPTURE_MAX_RETRIES
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk()
        sdk_payment.capture.side_effect = PaymentClientError('boom')
        capture_payment_task.push_request(retries=CAPTURE_MAX_RETRIES)
        try:
            with patch('payments.services.YooKassaService', return_value=svc):
                capture_payment_task.run(str(payment.id))  # no raise
        finally:
            capture_payment_task.pop_request()
        payment.refresh_from_db()
        assert payment.capture_state == Payment.CaptureState.CAPTURE_FAILED
        assert payment.status == Payment.Status.AUTHORIZED  # still held


# ---------------------------------------------------------------------------
# Booking cancel ⇒ hold released (acceptance #5)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCancelReleasesHold:
    def test_cancel_calls_yookassa_cancel(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk()
        with patch('payments.services.YooKassaService', return_value=svc):
            CancelBookingService().execute(CancelBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                initiator_role='client',
                reason='',
            ))
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        sdk_payment.cancel.assert_called_once_with(
            'yk_hold_001', f'cancel-{payment.id}',
        )
        payment.refresh_from_db()
        assert payment.capture_state == Payment.CaptureState.CANCELED

    def test_provider_outage_does_not_block_cancellation(
        self, client_user, specialist_user, service,
    ):
        from payments.exceptions import PaymentClientError
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        svc, sdk_payment = _svc_with_mocked_sdk()
        sdk_payment.cancel.side_effect = PaymentClientError('down')
        with patch('payments.services.YooKassaService', return_value=svc):
            CancelBookingService().execute(CancelBookingDTO(
                booking_id=appt.id,
                initiator_user_id=client_user.id,
                initiator_role='client',
                reason='',
            ))
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        payment.refresh_from_db()
        # Left for reconciliation — not silently dropped, not blocking.
        assert payment.capture_state == Payment.CaptureState.SCHEDULED


# ---------------------------------------------------------------------------
# Webhook capture_state transitions (D9)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestWebhookCaptureState:
    def _webhook(self, event, provider_id, mock_info):
        api = APIClient()
        api.defaults['HTTP_X_APP_TYPE'] = 'client'
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(
                get_payment_info=MagicMock(return_value=mock_info),
            ),
        ):
            return api.post(WEBHOOK_URL, {
                'event': event, 'object': {'id': provider_id},
            }, format='json')

    def test_waiting_for_capture_sets_scheduled_and_expires(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'awaiting_payment',
        )
        payment = Payment.objects.create(
            appointment=appt, amount=appt.price,
            status=Payment.Status.PENDING, provider='yookassa',
            provider_payment_id='yk_hold_001',
        )
        expires = timezone.now() + timedelta(hours=2)
        r = self._webhook('payment.waiting_for_capture', 'yk_hold_001', {
            'provider_payment_id': 'yk_hold_001',
            'status': 'waiting_for_capture',
            'paid': False,
            'expires_at': expires,
            'refunded_amount': Decimal('0'),
        })
        assert r.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.AUTHORIZED
        assert payment.capture_state == Payment.CaptureState.SCHEDULED
        assert payment.yookassa_expires_at == expires

    def test_succeeded_sets_captured_pending_settlement(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'confirmed',
        )
        payment = _held_payment(appt)
        r = self._webhook('payment.succeeded', 'yk_hold_001', {
            'provider_payment_id': 'yk_hold_001',
            'status': 'succeeded',
            'paid': True,
            'expires_at': None,
            'refunded_amount': Decimal('0'),
        })
        assert r.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PAID
        assert payment.capture_state == (
            Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
        )
        assert payment.captured_at is not None


# ---------------------------------------------------------------------------
# Manual capture re-run (management command, ADR §2)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestRetryCaptureCommand:
    def test_retries_stuck_payment_sync(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'completed',
        )
        payment = _held_payment(appt)
        payment.capture_state = Payment.CaptureState.CAPTURE_FAILED
        payment.save()
        svc, sdk_payment = _svc_with_mocked_sdk()
        with patch('payments.services.YooKassaService', return_value=svc):
            call_command('retry_capture', '--sync')
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PAID
        assert payment.capture_state == (
            Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
        )

    def test_ignores_non_stuck_payments(
        self, client_user, specialist_user, service,
    ):
        appt = _make_appointment(
            client_user, specialist_user, service, 'completed',
        )
        payment = _held_payment(appt)
        payment.status = Payment.Status.PAID
        payment.capture_state = Payment.CaptureState.CAPTURED_PENDING_SETTLEMENT
        payment.save()
        svc, sdk_payment = _svc_with_mocked_sdk()
        with patch('payments.services.YooKassaService', return_value=svc):
            call_command('retry_capture', '--sync')
        sdk_payment.capture.assert_not_called()


# ---------------------------------------------------------------------------
# AYLA-DEC-0010 (W1 side): online-paid ⇒ exactly the 90₽ platform fee
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSingleFeeInvariantW1Side:
    def test_online_paid_booking_has_flat_90_fee(
        self, client_user, specialist_user, service,
    ):
        """An online-paid completed booking carries exactly one 90₽
        platform fee on its Payment row. BookingFee must NOT exist for
        it — that side is W2's billing code (joint invariant test in
        Phase C); here we pin the W1 half: fee lives on the split only."""
        from appointments.application.dto import CreateBookingDTO
        from appointments.application.services.create_booking_service import (
            CreateBookingService,
        )
        result = CreateBookingService().execute(CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist_user.specialist_profile.id,
            service_id=service.id,
            start_at=(timezone.now() + timedelta(hours=3)).replace(
                second=0, microsecond=0,
            ),
            idempotency_key=str(uuid.uuid4()),
        ))
        payment = Payment.objects.get(appointment_id=result.booking_id)
        assert payment.platform_fee == Decimal('90.00')
        assert payment.specialist_income == (
            payment.amount - Decimal('90.00')
        )
