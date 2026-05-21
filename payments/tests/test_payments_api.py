"""Tests for DRF-73: Payments API — YooKassa integration."""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from appointments.models import Appointment
from payments.models import Payment
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
        from payments.exceptions import PaymentClientError

        appt = _make_appointment(client_user, specialist_user, service)
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(
                create_payment=MagicMock(
                    side_effect=PaymentClientError('upstream 500'),
                ),
            ),
        ):
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
            }, format='json')

        assert response.status_code == status.HTTP_502_BAD_GATEWAY
        assert response.data['error']['code'] == 'PAYMENT_PROVIDER_ERROR'

    def test_create_payment_config_error_returns_503(
        self, client_app, client_user, specialist_user, service,
    ):
        """PaymentConfigError (missing creds) maps to 503 — distinct
        from 502 transient provider failures so ops alerts route
        correctly."""
        from payments.exceptions import PaymentConfigError

        appt = _make_appointment(client_user, specialist_user, service)
        with patch(
            'payments.views._get_yookassa',
            side_effect=PaymentConfigError('SHOP_ID empty'),
        ):
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
            }, format='json')

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.data['error']['code'] == 'SERVICE_UNAVAILABLE'

    def test_create_payment_passes_fiscal_receipt(
        self, client_app, client_user, specialist_user, service,
    ):
        """54-ФЗ: each create_payment must include a receipt payload so
        YooKassa relays it to the OFD. Without this the merchant is
        non-compliant in production RF deployments."""
        appt = _make_appointment(client_user, specialist_user, service)
        mock_create = _mock_yookassa_create()
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(create_payment=mock_create),
        ):
            response = client_app.post(CREATE_URL, {
                'appointment_id': str(appt.id),
            }, format='json')

        assert response.status_code == status.HTTP_201_CREATED
        # Receipt was passed to YooKassaService.create_payment.
        kwargs = mock_create.call_args.kwargs
        assert "receipt" in kwargs
        receipt = kwargs["receipt"]
        # Customer must have at least phone (User.phone is mandatory).
        assert receipt["customer"]["phone"] == client_user.phone
        # Item description carries the service name capped at 128 chars.
        item = receipt["items"][0]
        assert item["description"] == "Маникюр"
        assert item["amount"]["value"] == "2000.00"
        assert item["amount"]["currency"] == "RUB"
        assert item["payment_subject"] == "service"
        assert item["payment_mode"] == "full_payment"
        # Default VAT code from settings (1 = без НДС for samozanyatye).
        assert item["vat_code"] == 1


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
        from appointments.models import OutboxEvent

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

        # N2: webhook writes a PAYMENT_CONFIRMED OutboxEvent in the same
        # transaction so the notifications dispatcher fires the
        # `payment_paid` push within one beat (~10s).
        events = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.PAYMENT_CONFIRMED,
        )
        assert events.count() == 1
        assert events.first().payload['payment_id'] == str(payment.id)
        assert events.first().payload['appointment_id'] == str(payment.appointment_id)

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

    def test_webhook_refund_succeeded_writes_outbox(
        self, anon_app, client_user, specialist_user, service,
    ):
        """Refund webhook writes a PAYMENT_REFUNDED OutboxEvent so the
        notifications dispatcher can push the refund-confirmation."""
        from appointments.models import OutboxEvent

        payment, _ = self._create_payment_with_appointment(
            client_user, specialist_user, service,
        )
        payment.status = Payment.Status.PAID
        payment.save()

        mock_info = {
            'provider_payment_id': 'wh_pay_001',
            'status': 'succeeded',
            'refunded_amount': payment.amount,
        }
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(
                get_payment_info=MagicMock(return_value=mock_info),
            ),
        ):
            response = anon_app.post(WEBHOOK_URL, {
                'event': 'refund.succeeded',
                'object': {'id': 'wh_pay_001'},
            }, format='json')

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == Payment.Status.REFUNDED

        events = OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.PAYMENT_REFUNDED,
        )
        assert events.count() == 1
        evt = events.first()
        assert evt.payload['payment_id'] == str(payment.id)
        assert evt.payload['is_partial'] is False

    def test_webhook_partial_refund_marks_outbox_partial(
        self, anon_app, client_user, specialist_user, service,
    ):
        from appointments.models import OutboxEvent

        payment, _ = self._create_payment_with_appointment(
            client_user, specialist_user, service,
        )
        payment.status = Payment.Status.PAID
        payment.save()

        # Refund less than full amount.
        partial = payment.amount / Decimal('2')
        mock_info = {
            'provider_payment_id': 'wh_pay_001',
            'status': 'succeeded',
            'refunded_amount': partial,
        }
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(
                get_payment_info=MagicMock(return_value=mock_info),
            ),
        ):
            anon_app.post(WEBHOOK_URL, {
                'event': 'refund.succeeded',
                'object': {'id': 'wh_pay_001'},
            }, format='json')

        payment.refresh_from_db()
        assert payment.status == Payment.Status.PARTIALLY_REFUNDED
        evt = OutboxEvent.objects.get(
            topic=OutboxEvent.Topic.PAYMENT_REFUNDED,
        )
        assert evt.payload['is_partial'] is True

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
        """Standard nginx-only setup (TRUSTED_PROXY_COUNT=1): nginx appends
        the TCP source it received the request from to XFF. With YooKassa
        sending us a request directly, that's the only XFF entry — and
        Django reads xff[-1]."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        settings.YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT = 1
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            REMOTE_ADDR='10.0.0.1',                # nginx
            HTTP_X_FORWARDED_FOR='185.71.76.10',   # what nginx appended (=YooKassa IP)
        )
        assert response.status_code == status.HTTP_200_OK

    def test_xff_spoofing_rejected(self, anon_app, settings):
        """Regression for the leftmost-XFF bypass: an attacker who can
        reach nginx sets ``X-Forwarded-For: <yookassa_ip>``; nginx
        appends its own TCP source (the attacker), so Django sees
        ``XFF = "yookassa_ip, attacker_ip"``. Reading the leftmost
        entry would let this through; reading the rightmost (xff[-1])
        is the attacker's real IP, which fails the allowlist.
        """
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        settings.YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT = 1
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'wh_spoof'}},
            format='json',
            REMOTE_ADDR='10.0.0.1',                                  # nginx
            HTTP_X_FORWARDED_FOR='185.71.76.10, 1.2.3.4',            # attacker spoof, then nginx-appended attacker IP
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data['error']['code'] == 'PERMISSION_DENIED'

    def test_two_trusted_proxies_reads_correct_depth(
        self, anon_app, settings,
    ):
        """CDN + nginx setup (TRUSTED_PROXY_COUNT=2): each proxy appends
        once, so the YooKassa IP sits at index -2. xff[-1] would be the
        CDN's IP and would fail the allowlist."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ['185.71.76.0/27']
        settings.YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT = 2
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            REMOTE_ADDR='10.0.0.1',                              # nginx
            HTTP_X_FORWARDED_FOR='185.71.76.10, 203.0.113.5',    # YooKassa, then CDN
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


@pytest.mark.django_db
class TestWebhookBasicAuth:
    """Basic Auth as second authentication layer (Phase A.2 — refactor).

    YooKassa supports `https://user:pass@host/...` URLs in their webhook
    config. When YOOKASSA_WEBHOOK_BASIC_AUTH_USER/PASS are set, the view
    rejects requests without matching credentials. When unset, behaviour
    is unchanged (IP allowlist + re-fetch alone).
    """

    def _build_basic_auth_header(self, user: str, password: str) -> str:
        import base64
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return f"Basic {token}"

    def test_passes_when_creds_correct(self, anon_app, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = 'yookassa'
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = 'secret-pass-1'
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            HTTP_AUTHORIZATION=self._build_basic_auth_header(
                'yookassa', 'secret-pass-1',
            ),
        )
        assert response.status_code == status.HTTP_200_OK

    def test_rejects_when_creds_wrong(self, anon_app, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = 'yookassa'
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = 'secret-pass-1'
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            HTTP_AUTHORIZATION=self._build_basic_auth_header(
                'yookassa', 'wrong-pass',
            ),
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_rejects_when_creds_required_but_missing(
        self, anon_app, settings,
    ):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = 'yookassa'
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = 'secret-pass-1'
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_skips_when_creds_unset(self, anon_app, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = ''
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = ''
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
        )
        assert response.status_code == status.HTTP_200_OK

    def test_malformed_basic_header_rejected(self, anon_app, settings):
        """Header starts with ``Basic`` but the rest is not valid base64 —
        view returns 403 (not 500). Using ``Bearer`` would trip JWT auth
        layer first → 401 before our check runs."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = 'yookassa'
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = 'secret-pass-1'
        response = anon_app.post(
            WEBHOOK_URL,
            {'event': 'payment.succeeded', 'object': {'id': 'unknown'}},
            format='json',
            HTTP_AUTHORIZATION='Basic !!!not-valid-base64!!!',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


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
