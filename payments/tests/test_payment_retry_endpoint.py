"""Tests for POST /api/v1/payments/{id}/retry/ — W2 payment_failed skill."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="retry_client", password="x", role="client",
        phone="+79991700000",
    )


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="retry_spec", password="x", role="specialist",
        phone="+79991700001",
    )


@pytest.fixture
def specialist(specialist_user):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.display_name = "Retry Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Retry", slug="retry-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="Retry Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


@pytest.fixture
def appointment(client_user, specialist, service):
    from uuid import uuid4
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    return appt


@pytest.fixture
def failed_payment(appointment):
    """A Payment row in FAILED state, ready for retry."""
    return Payment.objects.create(
        appointment=appointment,
        amount=appointment.price,
        status=Payment.Status.FAILED,
        specialist_income=Decimal("1380.00"),
        platform_fee=Decimal("120.00"),
        provider="yookassa",
        provider_payment_id="failed-yk-id-001",
        provider_client_secret="https://yookassa/old/expired",
    )


def _client_as(user) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


@pytest.mark.django_db
class TestPaymentRetryHappyPath:
    def test_retry_failed_payment_creates_new_session(
        self, client_user, failed_payment, appointment,
    ):
        mock_result = {
            'specialist_income': Decimal("1380.00"),
            'platform_fee': Decimal("120.00"),
            'provider_payment_id': "yk-retry-001",
            'confirmation_url': "https://yookassa/new/retry",
        }
        # PaymentRetryService now constructs YooKassaService lazily
        # inside execute() AFTER validation, so we patch the class
        # itself rather than the (now unused) ``payments.views``
        # ``_get_yookassa`` indirection.
        with patch(
            "payments.services.YooKassaService",
        ) as mock_yk_cls:
            mock_yk_cls.return_value.create_payment.return_value = mock_result
            r = _client_as(client_user).post(
                f"/api/v1/payments/{failed_payment.id}/retry/",
                {"return_url": "https://ayla.app/success"},
                format="json",
            )
        assert r.status_code == 201, r.data
        body = r.data.get("data") or r.data
        assert body["confirmation_url"] == "https://yookassa/new/retry"
        assert body["amount"] == 1500.0

        # Old payment row preserved as audit.
        failed_payment.refresh_from_db()
        assert failed_payment.status == Payment.Status.FAILED

        # New payment row created in PENDING. The appointment fixture
        # auto-creates one Payment via CreateBookingService, the
        # failed_payment fixture adds another, and retry adds a third.
        # Match by the mock's provider_payment_id to find the new one.
        new_payment = Payment.objects.get(
            appointment=appointment, provider_payment_id="yk-retry-001",
        )
        assert new_payment.status == Payment.Status.PENDING


@pytest.mark.django_db
class TestPaymentRetryStateGuards:
    def test_404_when_payment_belongs_to_other_user(
        self, client_user, specialist_user, failed_payment,
    ):
        """Cross-client access — 404, not 403."""
        other_client = User.objects.create_user(
            username="retry_other", password="x", role="client",
            phone="+79991700002",
        )
        r = _client_as(other_client).post(
            f"/api/v1/payments/{failed_payment.id}/retry/",
            {"return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 404

    def test_409_when_payment_already_paid(
        self, client_user, failed_payment,
    ):
        """Cannot retry a payment that already succeeded."""
        failed_payment.status = Payment.Status.PAID
        failed_payment.save(update_fields=["status"])
        r = _client_as(client_user).post(
            f"/api/v1/payments/{failed_payment.id}/retry/",
            {"return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "INVALID_STATUS"

    def test_409_when_payment_pending(self, client_user, failed_payment):
        """Cannot retry a pending payment — use the original
        confirmation URL or wait for it to fail first."""
        failed_payment.status = Payment.Status.PENDING
        failed_payment.save(update_fields=["status"])
        r = _client_as(client_user).post(
            f"/api/v1/payments/{failed_payment.id}/retry/",
            {"return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 409

    def test_409_when_appointment_already_cancelled(
        self, client_user, failed_payment, appointment,
    ):
        """Cancelled / completed appointment cannot accept new payment."""
        appointment.status = Appointment.Status.CANCELLED
        appointment.save(update_fields=["status"])
        r = _client_as(client_user).post(
            f"/api/v1/payments/{failed_payment.id}/retry/",
            {"return_url": "https://ayla.app/success"},
            format="json",
        )
        assert r.status_code == 409
