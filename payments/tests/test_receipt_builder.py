"""Tests for build_appointment_receipt — 54-ФЗ fiscal receipt builder.

The receipt payload is mandatory for production payments in RF; YooKassa
forwards it to the OFD. Tests pin the shape so a regression doesn't make
the merchant non-compliant.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from appointments.models import Appointment
from payments.services import build_appointment_receipt
from services.models import Service, ServiceCategory
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Cat receipt")


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username="r-spec", password="x", role="specialist",
        phone="+79991110000",
    )
    p = user.specialist_profile
    p.display_name = "Мастер"
    p.status = "active"
    p.save()
    return user


@pytest.fixture
def service(db, specialist_user, category):
    return Service.objects.create(
        specialist=specialist_user.specialist_profile,
        category=category,
        name="Маникюр",
        price=Decimal("2000.00"),
        duration_minutes=60,
        is_active=True,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="r-client", password="x", role="client",
        phone="+79992220000",
    )


@pytest.fixture
def appointment(db, client_user, specialist_user, service):
    when = timezone.now() + timezone.timedelta(hours=2)
    return Appointment.objects.create(
        client=client_user,
        specialist=specialist_user.specialist_profile,
        service=service,
        start_datetime=when,
        end_datetime=when + timezone.timedelta(minutes=service.duration_minutes),
        price=service.price,
        status=Appointment.Status.PENDING,
        snapshot_service_name=service.name,
        snapshot_price=service.price,
        snapshot_duration_minutes=service.duration_minutes,
    )


class TestReceiptShape:
    def test_basic_receipt_has_spec_fields(self, appointment):
        r = build_appointment_receipt(appointment, Decimal("2000.00"))
        assert set(r.keys()) == {"customer", "items"}
        assert "phone" in r["customer"]
        assert len(r["items"]) == 1
        item = r["items"][0]
        assert set(item.keys()) >= {
            "description", "quantity", "amount",
            "vat_code", "payment_mode", "payment_subject",
        }
        assert item["amount"] == {"value": "2000.00", "currency": "RUB"}
        assert item["payment_mode"] == "full_payment"
        assert item["payment_subject"] == "service"
        assert item["quantity"] == "1.00"


class TestCustomer:
    def test_phone_only_when_no_email(self, appointment, client_user):
        client_user.email = ""
        client_user.save()
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["customer"] == {"phone": client_user.phone}

    def test_phone_and_email_when_both_set(self, appointment, client_user):
        client_user.email = "anna@example.com"
        client_user.save()
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["customer"] == {
            "phone": client_user.phone, "email": "anna@example.com",
        }

    def test_fallback_phone_when_user_has_no_contact(self, appointment, client_user):
        # Shouldn't happen at runtime (User.phone is mandatory) but the
        # builder must never produce an empty customer or YooKassa rejects.
        client_user.phone = ""
        client_user.email = ""
        client_user.save()
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["customer"] == {"phone": "+70000000000"}


class TestItemDescription:
    def test_uses_service_name(self, appointment):
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["items"][0]["description"] == "Маникюр"

    def test_truncates_long_name_to_128_chars(
        self, appointment, service,
    ):
        service.name = "A" * 200
        service.save()
        appointment.refresh_from_db()
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert len(r["items"][0]["description"]) == 128

    def test_falls_back_to_snapshot_when_service_missing(self, client_user):
        # Defensive path: if appointment.service_id is None (admin
        # cleanup, schema drift) the builder still produces a usable
        # description from the booking-time snapshot. Hard to provoke
        # via real ORM since the FK is NOT NULL — exercise via a fake
        # appointment-shaped object.
        from types import SimpleNamespace

        fake = SimpleNamespace(
            client=client_user,
            service_id=None,
            service=None,
            snapshot_service_name="Маникюр (snapshot)",
        )
        r = build_appointment_receipt(fake, Decimal("100"))
        assert r["items"][0]["description"] == "Маникюр (snapshot)"

    def test_empty_service_and_snapshot_falls_back_to_default(
        self, client_user,
    ):
        from types import SimpleNamespace

        fake = SimpleNamespace(
            client=client_user,
            service_id=None,
            service=None,
            snapshot_service_name="",
        )
        r = build_appointment_receipt(fake, Decimal("100"))
        assert r["items"][0]["description"] == "Услуга"


class TestVatCode:
    def test_default_vat_code_is_1(self, appointment):
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["items"][0]["vat_code"] == 1

    def test_settings_override(self, appointment, settings):
        settings.YOOKASSA_VAT_CODE = 4  # НДС 20%
        r = build_appointment_receipt(appointment, Decimal("100"))
        assert r["items"][0]["vat_code"] == 4


class TestAmount:
    def test_two_decimal_places(self, appointment):
        r = build_appointment_receipt(appointment, Decimal("1500"))
        assert r["items"][0]["amount"]["value"] == "1500.00"

    def test_decimal_amount_preserved(self, appointment):
        r = build_appointment_receipt(appointment, Decimal("1234.56"))
        assert r["items"][0]["amount"]["value"] == "1234.56"
