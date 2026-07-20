"""C7.3 — on-demand payment status read model (AMD-016 semantics).

GET /api/v1/internal/payments/{payment_id}/ (Bearer):
- field shape per contract: payment_id / status / capture_state /
  amount (2dp string) / captured_at / expires_at / last_webhook_event_id;
- capture_state = ACTUAL state at response time in the C7 vocabulary
  (pending → authorized → captured / canceled / failed / refunded) —
  never the raw provider `waiting_for_capture`;
- 404 PAYMENT_NOT_FOUND, info-hidden.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-c73"


def _url(payment_id) -> str:
    return f"/api/v1/internal/payments/{payment_id}/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def payment(db):
    u = User.objects.create_user(
        username="c73_client", password="x", role="client",
        phone="+79994007001",
    )
    sp_user = User.objects.create_user(
        username="c73_spec", password="x", role="specialist",
        phone="+79994007002",
    )
    specialist = SpecialistProfile.objects.get(user=sp_user)
    cat = ServiceCategory.objects.create(name="C73 Cat", slug="c73-cat")
    service = Service.objects.create(
        specialist=specialist, category=cat, name="C73 Service",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )
    now = datetime.now(tz=timezone.utc)
    appt = Appointment.objects.create(
        client=u, specialist=specialist, service=service,
        start_datetime=now + timedelta(hours=3),
        end_datetime=now + timedelta(hours=4),
        status=Appointment.Status.CONFIRMED, price=service.price,
    )
    return Payment.objects.create(
        appointment=appt, amount=service.price,
        status=Payment.Status.AUTHORIZED,
        specialist_income=Decimal("1910.00"), platform_fee=Decimal("90.00"),
        provider="yookassa", provider_payment_id="yk_c73_1",
        capture_state=Payment.CaptureState.SCHEDULED,
        yookassa_expires_at=now + timedelta(days=7),
        last_webhook_event_id="payment.waiting_for_capture:yk_c73_1",
    )


def _api(*, bearer=VALID_TOKEN):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


@pytest.mark.django_db
class TestPaymentStatusRead:
    def test_auth_boundary(self, payment):
        assert _api(bearer=None).get(_url(payment.id)).status_code == 403
        assert _api(bearer="nope").get(_url(payment.id)).status_code == 403

    def test_404_unknown_payment_info_hidden(self):
        r = _api().get(_url(uuid.uuid4()))
        assert r.status_code == 404
        assert r.data["error"]["code"] == "PAYMENT_NOT_FOUND"

    def test_field_shape_and_types(self, payment):
        r = _api().get(_url(payment.id))
        assert r.status_code == 200, r.data
        data = r.data["data"]
        assert data["payment_id"] == str(payment.id)
        assert data["status"] == "authorized"
        # Hold active, capture planned → C7 vocabulary "authorized".
        assert data["capture_state"] == "authorized"
        assert data["amount"] == "2000.00"
        assert data["captured_at"] is None
        assert data["expires_at"] == payment.yookassa_expires_at.isoformat()
        assert data["last_webhook_event_id"] == (
            "payment.waiting_for_capture:yk_c73_1"
        )

    @pytest.mark.parametrize("status,capture_state,expected", [
        (Payment.Status.PENDING, "", "pending"),
        (Payment.Status.AUTHORIZED, "scheduled", "authorized"),
        (Payment.Status.PAID, "captured_pending_settlement", "captured"),
        (Payment.Status.FAILED, "canceled", "canceled"),
        (Payment.Status.FAILED, "capture_failed", "failed"),
        (Payment.Status.REFUNDED, "refunded", "refunded"),
        (Payment.Status.PARTIALLY_REFUNDED, "refunded", "refunded"),
    ])
    def test_capture_state_maps_actual_lifecycle(
        self, payment, status, capture_state, expected,
    ):
        """AMD-016: the field reports the ACTUAL state in the C7
        vocabulary — never the raw provider waiting_for_capture."""
        payment.status = status
        payment.capture_state = capture_state
        if status == Payment.Status.PAID:
            payment.captured_at = datetime.now(tz=timezone.utc)
        payment.save()
        r = _api().get(_url(payment.id))
        assert r.status_code == 200
        assert r.data["data"]["capture_state"] == expected

    def test_captured_at_serialized_when_present(self, payment):
        payment.status = Payment.Status.PAID
        payment.capture_state = "captured_pending_settlement"
        payment.captured_at = datetime.now(tz=timezone.utc)
        payment.save()
        r = _api().get(_url(payment.id))
        assert r.data["data"]["captured_at"] == (
            payment.captured_at.isoformat()
        )
