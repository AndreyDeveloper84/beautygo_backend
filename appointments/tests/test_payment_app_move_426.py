"""Regression tests for #426 PR 1 — Payment moved from appointments → payments.

PR 1 is the state-only half of the move (Strategy A: SeparateDatabaseAndState
+ db_table pinned to the legacy name). The physical table stays as
`appointments_payment` until PR 2 lands `AlterModelTable`. These tests
pin the invariants that PR 1 must preserve so a follow-up refactor
cannot silently regress them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import Appointment
from payments.models import Payment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="r426_specialist", password="pass", role="specialist",
        phone="+79991110011",
    )


@pytest.fixture
def specialist(specialist_user):
    profile = SpecialistProfile.objects.get(user=specialist_user)
    profile.display_name = "R426 Specialist"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="R426 Cat", slug="r426-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="R426 Service",
        price=Decimal("1500.00"),
        duration_minutes=45,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="r426_client", password="pass", role="client",
        phone="+79991110022",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


class TestPaymentAppMoveState:
    """The move is state-only in PR 1. Confirm Django registry agrees."""

    def test_payment_app_label_is_payments(self):
        assert Payment._meta.app_label == "payments"

    def test_payment_table_still_named_appointments_payment(self):
        # PR 2 will flip this to 'payments_payment' via AlterModelTable.
        # Until then the physical table is unmoved.
        assert Payment._meta.db_table == "appointments_payment"

    def test_appointment_reverse_manager_resolves_payment(self):
        """`appointment.payments.all()` must still return Payment rows —
        the FK lives on Payment, with related_name='payments'."""
        field = Appointment._meta.get_field("payments")
        assert field.related_model is Payment

    def test_payment_status_choices_unchanged(self):
        """Lifecycle enum is a serialised contract (YooKassa webhooks
        + outbox topics depend on the exact string values). Pinning it
        here makes a future innocent edit fail loud."""
        assert Payment.Status.PENDING == "pending"
        assert Payment.Status.AUTHORIZED == "authorized"
        assert Payment.Status.PAID == "paid"
        assert Payment.Status.FAILED == "failed"
        assert Payment.Status.REFUNDED == "refunded"
        assert Payment.Status.PARTIALLY_REFUNDED == "partially_refunded"


@pytest.mark.django_db
class TestPaymentCrudAcrossAppMove:
    """The booking service creates an Appointment + Payment in the same
    transaction. After the move, the resolution still has to work end-
    to-end — this is the regression test for the cross-app FK."""

    def test_create_booking_creates_payment_via_reverse_manager(
        self, client_user, specialist, service,
    ):
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=str(uuid4()),
        )
        CreateBookingService().execute(dto)

        appt = Appointment.objects.get()
        assert appt.payments.count() == 1
        payment = appt.payments.first()
        # Forward FK works in both directions.
        assert payment.appointment_id == appt.id
        # Same UUID PK shape as before the move.
        assert payment.id is not None
        assert payment.amount == Decimal("1500.00")

    def test_payment_net_amount_property_survives_move(
        self, client_user, specialist, service,
    ):
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(4),
            idempotency_key=str(uuid4()),
        )
        CreateBookingService().execute(dto)
        payment = Payment.objects.get()
        # Simulate a partial refund and re-read the @property.
        payment.refunded_amount = Decimal("500.00")
        payment.save(update_fields=["refunded_amount"])
        payment.refresh_from_db()
        assert payment.net_amount == Decimal("1000.00")
