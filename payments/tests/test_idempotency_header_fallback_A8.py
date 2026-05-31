"""A8 — Idempotency header fallback (joint with W4).

Locks the contract between Ayla `/api/v1/payments/create/` and the
bot-platform `AylaPaymentsClient`. Codex P0-3 audit found that bot
currently sends ``Idempotence-Key`` (legacy YooKassa SDK spelling)
while Ayla only honours ``X-Idempotency-Key``. Net result: a bot
retry generates a fresh ``str(uuid4())`` per attempt → YooKassa
creates one payment per call → customer sees N pending charges.

The fix accepts all three spellings on the Ayla side (X-Idempotency-Key
canonical, Idempotence-Key bot/YooKassa-historical, Idempotency-Key
RFC-style). W4 owns the rename on the bot-platform side; once that
ships, the fallback can be removed in a later PR — until then the
defensive accept-all guards live payment integrity.

We assert the resolved idempotency key reaches YooKassa rather than
generating a fresh UUID because that's the symptom that matters:
"same key in → same payment out", regardless of which header the
caller used.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import User

CREATE_URL = '/api/v1/payments/create/'


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name='Aftercare')


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username='a8_specialist', password='pass',
        role='specialist', phone='+79990800010',
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
        username='a8_client', password='pass',
        role='client', phone='+79990800001',
    )


@pytest.fixture
def client_app(db, client_user):
    api = APIClient()
    api.defaults['HTTP_X_APP_TYPE'] = 'client'
    api.force_authenticate(user=client_user)
    return api


def _make_appointment(client_user, specialist_user, service):
    now = timezone.now()
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist_user.specialist_profile,
        service=service,
        start_datetime=now + timezone.timedelta(hours=2),
        end_datetime=now + timezone.timedelta(hours=3),
        status='pending',
        price=service.price,
    )


def _yookassa_mock():
    """Spy on the kwargs YooKassaService.create_payment receives."""
    mock = MagicMock()
    mock.return_value = {
        'provider_payment_id': str(uuid.uuid4()),
        'confirmation_url': 'https://yookassa.ru/pay/a8',
        'status': 'pending',
        'platform_fee': Decimal('160.00'),
        'specialist_income': Decimal('1840.00'),
    }
    return mock


@pytest.mark.django_db
class TestIdempotencyHeaderFallback:
    """Same key reaches YooKassa regardless of which header carries it."""

    KEY = 'idemp-key-a8-001'

    def _post_with(self, client_app, appt_id, header_name, header_value):
        # APIClient .post() passes any kwarg as a META override —
        # APIClient knows to translate HTTP_FOO_BAR → header FOO_BAR.
        meta_name = f"HTTP_{header_name.replace('-', '_').upper()}"
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(create_payment=_yookassa_mock()),
        ) as mock_gw:
            response = client_app.post(
                CREATE_URL,
                {'appointment_id': str(appt_id)},
                format='json',
                **{meta_name: header_value},
            )
            return response, mock_gw

    def test_canonical_x_idempotency_key_wins(
        self, client_app, client_user, specialist_user, service,
    ):
        appt = _make_appointment(client_user, specialist_user, service)
        response, mock_gw = self._post_with(
            client_app, appt.id, 'X-Idempotency-Key', self.KEY,
        )
        assert response.status_code == 201
        call_kwargs = mock_gw.return_value.create_payment.call_args.kwargs
        assert call_kwargs['idempotency_key'] == self.KEY

    def test_idempotence_key_legacy_spelling_honoured(
        self, client_app, client_user, specialist_user, service,
    ):
        # Bot-platform's current spelling — codex P0-3 root cause for
        # the duplicate-payment-on-retry bug. Without the fallback the
        # view would generate a fresh uuid here.
        appt = _make_appointment(client_user, specialist_user, service)
        response, mock_gw = self._post_with(
            client_app, appt.id, 'Idempotence-Key', self.KEY,
        )
        assert response.status_code == 201
        call_kwargs = mock_gw.return_value.create_payment.call_args.kwargs
        assert call_kwargs['idempotency_key'] == self.KEY

    def test_idempotency_key_rfc_spelling_honoured(
        self, client_app, client_user, specialist_user, service,
    ):
        # RFC draft uses "Idempotency-Key" (no X- prefix). Accepted for
        # forward-compat with clients that follow the IETF spelling.
        appt = _make_appointment(client_user, specialist_user, service)
        response, mock_gw = self._post_with(
            client_app, appt.id, 'Idempotency-Key', self.KEY,
        )
        assert response.status_code == 201
        call_kwargs = mock_gw.return_value.create_payment.call_args.kwargs
        assert call_kwargs['idempotency_key'] == self.KEY

    def test_canonical_wins_when_multiple_headers_present(
        self, client_app, client_user, specialist_user, service,
    ):
        # A migrating client that emits both during the transition
        # window must converge on the canonical value. Without this
        # ordering, the bot keeps shipping its legacy key even after
        # the rename and the contract effectively never moves.
        appt = _make_appointment(client_user, specialist_user, service)
        with patch(
            'payments.views._get_yookassa',
            return_value=MagicMock(create_payment=_yookassa_mock()),
        ) as mock_gw:
            response = client_app.post(
                CREATE_URL,
                {'appointment_id': str(appt.id)},
                format='json',
                HTTP_X_IDEMPOTENCY_KEY='canonical',
                HTTP_IDEMPOTENCE_KEY='legacy',
            )
        assert response.status_code == 201
        call_kwargs = mock_gw.return_value.create_payment.call_args.kwargs
        assert call_kwargs['idempotency_key'] == 'canonical'

    def test_missing_header_falls_back_to_random_uuid(
        self, client_app, client_user, specialist_user, service,
    ):
        # Last-line behaviour — without any header, a unique key is
        # generated per call. Pinned so a future refactor that drops
        # the random-uuid fallback does not break first-time mobile
        # callers that never pass the header.
        appt = _make_appointment(client_user, specialist_user, service)
        response, mock_gw = self._post_with(
            client_app, appt.id, 'X-Idempotency-Key', '',
        )
        # APIClient with empty value still sends the header; META has
        # the empty string. The helper treats empty as missing (the
        # ``or`` short-circuits) and falls through to uuid4.
        assert response.status_code == 201
        call_kwargs = mock_gw.return_value.create_payment.call_args.kwargs
        # uuid4 stringified is 36 chars with dashes — pin the shape.
        assert len(call_kwargs['idempotency_key']) == 36
