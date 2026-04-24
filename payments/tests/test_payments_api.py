"""Tests for DRF-73: Payments API — YooKassa integration."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from appointments.models import Appointment, Payment
from services.models import Service, ServiceCategory
from users.models import User

CREATE_URL = '/api/v1/payments/create/'
WEBHOOK_URL = '/api/v1/payments/webhook/'


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Ногти')


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username='pay_specialist', password='pass',
        role='specialist', phone='+79990700010',
    )
    p = user.specialist_profile
    p.display_name = 'Мастер'
    p.status = 'active'
    p.is_available = True
    p.save()
    return user


@pytest.fixture
def service(db, specialist_user, category):
    return Service.objects.create(
        specialist=specialist_user.specialist_profile,
        category=category,
        name='Маникюр',
        price=Decimal('2000.00'),
        duration_minutes=60,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='pay_client', password='pass',
        role='client', phone='+79990700001',
    )


@pytest.fixture
def client_app(db, client_user):
    api = APIClient()
    api.defaults['HTTP_X_APP_TYPE'] = 'client'
    api.force_authenticate(user=client_user)
    return api


@pytest.fixture
def anon_app(db):
    api = APIClient()
    api.defaults['HTTP_X_APP_TYPE'] = 'client'
    return api


def _make_appointment(client_user, specialist_user, service, appt_status='pending'):
    now = timezone.now()
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist_user.specialist_profile,
        service=service,
        start_datetime=now + timezone.timedelta(hours=2),
        end_datetime=now + timezone.timedelta(hours=3),
        status=appt_status,
        price=service.price,
    )


def _mock_yookassa_create(confirmation_url='https://yookassa.ru/pay/test'):
    """Return a mock that simulates YooKassaService.create_payment."""
    mock = MagicMock()
    mock.return_value = {
        'provider_payment_id': str(uuid.uuid4()),
        'confirmation_url': confirmation_url,
        'status': 'pending',
        'platform_fee': Decimal('160.00'),
        'specialist_income': Decimal('1840.00'),
    }
    return mock


# ---------------------------------------------------------------------------
# POST /api/v1/payments/create/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPaymentCreate:

    def test_create_payment_success(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)

        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(create_payment=_mock_yookassa_create()),
        ):
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
                'return_url': 'https://beautygo.ru/success',
            }, format='json')

        assert response.status_code == status.HTTP_201_CREATED
        data = response.data['data']
        assert 'payment_id' in data
        assert 'confirmation_url' in data
        assert data['confirmation_url'] == 'https://yookassa.ru/pay/test'
        assert data['amount'] == 2000.0

        # Payment record created
        assert Payment.objects.filter(appointment=appt).count() == 1
        payment = Payment.objects.get(appointment=appt)
        assert payment.provider == 'yookassa'
        assert payment.amount == Decimal('2000.00')

        # Appointment moved to awaiting_payment
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.AWAITING_PAYMENT

    def test_create_payment_idempotent(self, client_app, client_user, specialist_user, service):
        """Second call returns existing pending payment."""
        appt = _make_appointment(client_user, specialist_user, service)
        provider_id = str(uuid.uuid4())
        conf_url = 'https://yookassa.ru/pay/existing'
        Payment.objects.create(
            appointment=appt,
            amount=appt.price,
            status=Payment.Status.PENDING,
            provider='yookassa',
            provider_payment_id=provider_id,
            provider_client_secret=conf_url,
        )

        with patch('payments.views._get_yookassa') as mock_gw:
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
            }, format='json')
            mock_gw.assert_not_called()

        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['confirmation_url'] == conf_url

    def test_create_payment_wrong_status(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service, appt_status='cancelled')
        response = client_app.post(CREATE_URL, {
            'appointment_id': str(appt.id),
        }, format='json')
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data['error']['code'] == 'INVALID_STATUS'

    def test_create_payment_not_own_appointment(
        self, client_app, specialist_user, service, db,
    ):
        other = User.objects.create_user(
            username='other_pay', password='pass', role='client', phone='+79990700099',
        )
        appt = _make_appointment(other, specialist_user, service)
        response = client_app.post(CREATE_URL, {
            'appointment_id': str(appt.id),
        }, format='json')
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_payment_nonexistent_appointment(self, client_app):
        response = client_app.post(CREATE_URL, {
            'appointment_id': str(uuid.uuid4()),
        }, format='json')
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_payment_unauthenticated(self, anon_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        response = anon_app.post(CREATE_URL, {
            'appointment_id': str(appt.id),
        }, format='json')
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
        )

    def test_create_payment_provider_error(
        self, client_app, client_user, specialist_user, service,
    ):
        appt = _make_appointment(client_user, specialist_user, service)
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(create_payment=MagicMock(side_effect=Exception('API error'))),
        ):
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
            }, format='json')

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.data['error']['code'] == 'PAYMENT_PROVIDER_ERROR'


# ---------------------------------------------------------------------------
# GET /api/v1/payments/{id}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPaymentDetail:

    def test_get_payment_client(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt, amount=appt.price,
            status=Payment.Status.PAID,
            provider='yookassa', provider_payment_id='pay_123',
        )
        url = f'/api/v1/payments/{payment.id}/'
        response = client_app.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['id'] == str(payment.id)
        # Spec fields present
        assert data['appointment_id'] == str(appt.id)
        assert data['client_id'] == str(client_user.id)
        assert data['external_id'] == 'pay_123'
        assert data['status'] == 'succeeded'  # paid → succeeded
        assert data['completed_at'] is not None
        assert data['provider'] == 'yookassa'
        # Internal fields NOT exposed
        assert 'provider_payment_id' not in data
        assert 'specialist_income' not in data
        assert 'platform_fee' not in data

    def test_get_payment_not_found(self, client_app):
        response = client_app.get(f'/api/v1/payments/{uuid.uuid4()}/')
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_payment_forbidden(self, client_user, specialist_user, service, db):
        other = User.objects.create_user(
            username='spy_client', password='pass', role='client', phone='+79990700088',
        )
        other_api = APIClient()
        other_api.defaults['HTTP_X_APP_TYPE'] = 'client'
        other_api.force_authenticate(user=other)

        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt, amount=appt.price,
            provider='yookassa', provider_payment_id='pay_456',
        )
        response = other_api.get(f'/api/v1/payments/{payment.id}/')
        assert response.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# POST /api/v1/payments/webhook/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPaymentWebhook:

    def _create_payment_with_appointment(self, client_user, specialist_user, service):
        appt = _make_appointment(
            client_user, specialist_user, service, appt_status='awaiting_payment',
        )
        return Payment.objects.create(
            appointment=appt,
            amount=appt.price,
            status=Payment.Status.PENDING,
            provider='yookassa',
            provider_payment_id='wh_pay_001',
        ), appt

    def test_webhook_waiting_for_capture(self, anon_app, client_user, specialist_user, service):
        payment, appt = self._create_payment_with_appointment(
            client_user, specialist_user, service,
        )
        mock_info = {
            'provider_payment_id': 'wh_pay_001',
            'status': 'waiting_for_capture',
            'paid': False,
            'refunded_amount': Decimal('0'),
        }
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(get_payment_info=MagicMock(return_value=mock_info)),
        ):
            response = anon_app.post(WEBHOOK_URL, {
                'event': 'payment.waiting_for_capture',
                'object': {'id': 'wh_pay_001'},
            }, format='json')

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.AUTHORIZED
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED

    def test_webhook_payment_succeeded(self, anon_app, client_user, specialist_user, service):
        payment, _ = self._create_payment_with_appointment(client_user, specialist_user, service)
        payment.status = Payment.Status.AUTHORIZED
        payment.save()

        mock_info = {
            'provider_payment_id': 'wh_pay_001',
            'status': 'succeeded',
            'paid': True,
            'refunded_amount': Decimal('0'),
        }
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(get_payment_info=MagicMock(return_value=mock_info)),
        ):
            response = anon_app.post(WEBHOOK_URL, {
                'event': 'payment.succeeded',
                'object': {'id': 'wh_pay_001'},
            }, format='json')

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PAID

    def test_webhook_payment_canceled(self, anon_app, client_user, specialist_user, service):
        payment, appt = self._create_payment_with_appointment(client_user, specialist_user, service)

        mock_info = {
            'provider_payment_id': 'wh_pay_001',
            'status': 'canceled',
            'paid': False,
            'refunded_amount': Decimal('0'),
        }
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(get_payment_info=MagicMock(return_value=mock_info)),
        ):
            response = anon_app.post(WEBHOOK_URL, {
                'event': 'payment.canceled',
                'object': {'id': 'wh_pay_001'},
            }, format='json')

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.FAILED
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED

    def test_webhook_idempotent(self, anon_app, client_user, specialist_user, service):
        """Same event ID processed only once."""
        payment, _ = self._create_payment_with_appointment(client_user, specialist_user, service)
        payment.last_webhook_event_id = 'payment.succeeded:wh_pay_001'
        payment.save()

        with patch('payments.views._get_yookassa') as mock_gw:
            response = anon_app.post(
                WEBHOOK_URL,
                {'event': 'payment.succeeded', 'object': {'id': 'wh_pay_001'}},
                format='json',
                HTTP_X_REQUEST_ID='payment.succeeded:wh_pay_001',
            )
            mock_gw.assert_not_called()

        assert response.status_code == 200
        assert response.data['status'] == 'duplicate'

    def test_webhook_unknown_payment(self, anon_app):
        response = anon_app.post(WEBHOOK_URL, {
            'event': 'payment.succeeded',
            'object': {'id': 'unknown_pay_xyz'},
        }, format='json')
        assert response.status_code == 200
        assert response.data['status'] == 'ok'

    def test_webhook_missing_fields(self, anon_app):
        response = anon_app.post(WEBHOOK_URL, {}, format='json')
        assert response.status_code == 200
        assert response.data['status'] == 'ignored'


@pytest.mark.django_db
class TestPaymentWebhookSecurity:
    """Regression tests for H1 — YooKassa webhook must reject unexpected
    source IPs when an allowlist is configured, and must cap amplification
    via throttling."""

    def test_rejects_unlisted_ip_when_allowlist_set(
        self, anon_app, client_user, specialist_user, service, settings,
    ):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'wh_pay_sec'}},
            format='json',
            REMOTE_ADDR='1.2.3.4',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data['error']['code'] == 'PERMISSION_DENIED'

    def test_accepts_listed_ip(self, anon_app, settings):
        """An IP inside the configured CIDR is not rejected for IP reason
        (unknown payment_id → 200 'ok' from the normal flow)."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            REMOTE_ADDR='185.71.76.5',
        )
        assert response.status_code == status.HTTP_200_OK

    def test_respects_x_forwarded_for_when_behind_proxy(
        self, anon_app, settings,
    ):
        """Real client IP lives in XFF when behind nginx; the check must
        honour that rather than rejecting the proxy's IP."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            REMOTE_ADDR='10.0.0.1',                # nginx
            HTTP_X_FORWARDED_FOR='185.71.76.10, 10.0.0.1',
        )
        assert response.status_code == status.HTTP_200_OK

    def test_empty_allowlist_permits_all(self, anon_app, settings):
        """Dev / initial-deploy mode: unset env permits all, with a warning
        logged (behaviour preserved from before H1)."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            REMOTE_ADDR='1.2.3.4',
        )
        assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# POST /api/v1/payments/{id}/refund/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPaymentRefund:

    def test_refund_full_success(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.AUTHORIZED,
            provider='yookassa',
            provider_payment_id='ref_pay_001',
        )
        url = f'/api/v1/payments/{payment.id}/refund/'

        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(refund_payment=MagicMock(
                return_value={'refund_id': 'ref_1', 'status': 'succeeded'},
            )),
        ):
            response = client_app.post(url, {}, format='json')

        assert response.status_code == status.HTTP_200_OK
        payment.refresh_from_db()
        assert payment.status == Payment.Status.REFUNDED
        assert payment.refunded_amount == Decimal('2000.00')

    def test_refund_partial(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.PAID,
            provider='yookassa',
            provider_payment_id='ref_pay_002',
        )
        url = f'/api/v1/payments/{payment.id}/refund/'

        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(refund_payment=MagicMock(
                return_value={'refund_id': 'ref_2', 'status': 'succeeded'},
            )),
        ):
            response = client_app.post(url, {'amount': '500.00'}, format='json')

        assert response.status_code == status.HTTP_200_OK
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PARTIALLY_REFUNDED
        assert payment.refunded_amount == Decimal('500.00')

    def test_refund_wrong_status(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.PENDING,
            provider='yookassa',
            provider_payment_id='ref_pay_003',
        )
        response = client_app.post(f'/api/v1/payments/{payment.id}/refund/', {}, format='json')
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data['error']['code'] == 'REFUND_NOT_ALLOWED'

    def test_refund_amount_exceeds_paid(self, client_app, client_user, specialist_user, service):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.PAID,
            provider='yookassa',
            provider_payment_id='ref_pay_004',
        )
        response = client_app.post(
            f'/api/v1/payments/{payment.id}/refund/',
            {'amount': '9999.00'},
            format='json',
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.data['error']['code'] == 'REFUND_AMOUNT_EXCEEDS_PAID'

    def test_refund_forbidden_other_client(
        self, client_user, specialist_user, service, db,
    ):
        other = User.objects.create_user(
            username='other_refund', password='pass', role='client', phone='+79990700077',
        )
        other_api = APIClient()
        other_api.defaults['HTTP_X_APP_TYPE'] = 'client'
        other_api.force_authenticate(user=other)

        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.AUTHORIZED,
            provider='yookassa',
            provider_payment_id='ref_pay_005',
        )
        response = other_api.post(f'/api/v1/payments/{payment.id}/refund/', {}, format='json')
        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestPaymentRefundThrottle:
    """L3 regression — refund endpoint must sit on the 'payment' 5/min
    bucket so refund floods don't burn YooKassa API quota."""

    def test_refund_throttled_at_6th_request(
        self, client_app, client_user, specialist_user, service,
    ):
        appt = _make_appointment(client_user, specialist_user, service)
        payment = Payment.objects.create(
            appointment=appt,
            amount=Decimal('2000.00'),
            status=Payment.Status.AUTHORIZED,
            provider='yookassa',
            provider_payment_id='ref_throttle_001',
        )
        url = f'/api/v1/payments/{payment.id}/refund/'

        # First 5 burn the bucket (any non-429 status is fine — success or
        # business error are both not-throttled).
        for i in range(5):
            response = client_app.post(url, {}, format='json')
            assert response.status_code != status.HTTP_429_TOO_MANY_REQUESTS, (
                f"Refund throttled on request #{i + 1}; expected first 5 to pass"
            )

        # 6th hits the cap.
        response = client_app.post(url, {}, format='json')
        assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
