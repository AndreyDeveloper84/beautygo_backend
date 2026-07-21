"""AMD-019 persistence option A (v1.13.0) — real salon-catalog bookings.

A SalonService booking persists with ``salon_service`` filled and
``service IS NULL`` (exactly-one CHECK), all snapshots stamped, the
full pipeline intact (idempotency, eligibility, statuses, payment,
outbox). Error cases keep the pre-existing shapes (422, no leak).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import Appointment, OutboxEvent
from payments.services import build_appointment_receipt
from services.models import (
    SalonService,
    Service,
    ServiceCategory,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-amd019p"
EXTERNAL_USER_ID = "bot:amd019p"
CREATE_URL = "/api/v1/internal/appointments/"
RECORDS_URL = "/api/v1/internal/me/bookings/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79996195001", is_proxy=True,
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="amd019p-t", name="AMD019P Tenant")


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="amd019p_spec", password="x", role="specialist",
        phone="+79996195002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "AMD019P Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="AMD019P Cat", slug="amd019p-cat")


@pytest.fixture
def salon(tenant, category):
    return SalonService.objects.create(
        tenant=tenant, category=category, name="AMD019P Salon",
        duration_minutes=60,
    )


@pytest.fixture
def salon_link(salon, specialist):
    return SpecialistService.objects.create(
        salon_service=salon, specialist=specialist,
        duration_minutes=None, price=Decimal("2000.00"),
        buffer_after_minutes=15, is_active=True,
    )


@pytest.fixture
def marketplace_service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="AMD019P Mkt",
        price=Decimal("1500.00"), duration_minutes=45, is_active=True,
    )


def _future(hours: int = 3):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).replace(
        second=0, microsecond=0,
    )


def _dto(customer, specialist, service_id, **kw):
    return CreateBookingDTO(
        client_id=customer.id,
        specialist_id=specialist.id,
        service_id=service_id,
        start_at=_future(3),
        idempotency_key=str(uuid4()),
        **kw,
    )


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


# ---------------------------------------------------------------------------
# Real salon booking (option A)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSalonBookingPersists:
    def test_salon_booking_created_awaiting_payment(
        self, customer, specialist, salon_link,
    ):
        """Full pipeline: persisted with salon_service set, service NULL,
        all snapshots stamped from the normalized resolver result,
        AWAITING_PAYMENT + pending Payment + outbox events."""
        result = CreateBookingService().execute(
            _dto(customer, specialist, salon_link.salon_service_id),
        )
        appt = Appointment.objects.get(id=result.booking_id)

        # Option A shape: exactly one typed reference.
        assert appt.salon_service_id == salon_link.salon_service_id
        assert appt.service_id is None

        # Snapshots from the resolver's normalized result (salon name,
        # salon duration, LINK price/buffer) — the historical source.
        assert appt.snapshot_service_name == salon_link.salon_service.name
        assert appt.snapshot_duration_minutes == 60
        assert appt.snapshot_price == Decimal("2000.00")
        assert appt.snapshot_platform_fee == Decimal("90.00")
        assert appt.snapshot_specialist_income == Decimal("1910.00")
        assert appt.price == Decimal("2000.00")

        # Pipeline intact.
        assert appt.status == Appointment.Status.AWAITING_PAYMENT
        payment = appt.payments.get()
        assert payment.status == "pending"
        assert payment.platform_fee == Decimal("90.00")
        topics = set(OutboxEvent.objects.values_list("topic", flat=True))
        assert OutboxEvent.Topic.BOOKING_CREATED in topics

    def test_salon_walk_in_confirmed_without_payment(
        self, customer, specialist, salon_link,
    ):
        """The no-prepayment path (D6) works for salon bookings too:
        CONFIRMED + no Payment + booking.confirmed emitted."""
        result = CreateBookingService().execute(_dto(
            customer, specialist, salon_link.salon_service_id,
            payment_required=False, confirm_immediately=True,
        ))
        appt = Appointment.objects.get(id=result.booking_id)
        assert appt.salon_service_id == salon_link.salon_service_id
        assert appt.service_id is None
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.payments.count() == 0
        topics = set(OutboxEvent.objects.values_list("topic", flat=True))
        assert OutboxEvent.Topic.BOOKING_CONFIRMED in topics

    def test_salon_booking_via_internal_api(
        self, customer, specialist, salon_link,
    ):
        body = {
            "client_id": str(customer.id),
            "specialist_id": str(specialist.id),
            "service_id": str(salon_link.salon_service_id),
            "start_datetime": _future(3).isoformat(),
        }
        r = _api().post(CREATE_URL, body, format="json")
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get(id=r.data["data"]["id"])
        assert appt.salon_service_id == salon_link.salon_service_id
        assert appt.service_id is None


@pytest.mark.django_db
class TestMarketplaceRegression:
    def test_marketplace_booking_has_service_not_salon(
        self, customer, specialist, marketplace_service,
    ):
        result = CreateBookingService().execute(
            _dto(customer, specialist, marketplace_service.id),
        )
        appt = Appointment.objects.get(id=result.booking_id)
        assert appt.service_id == marketplace_service.id
        assert appt.salon_service_id is None
        assert appt.snapshot_service_name == marketplace_service.name


@pytest.mark.django_db
class TestExactlyOneConstraint:
    def _base(self, customer, specialist, marketplace_service):
        now = datetime.now(tz=timezone.utc)
        return dict(
            client=customer, specialist=specialist,
            start_datetime=now + timedelta(hours=3),
            end_datetime=now + timedelta(hours=4),
            status=Appointment.Status.CONFIRMED, price=Decimal("100.00"),
        )

    def test_both_null_rejected(
        self, customer, specialist, marketplace_service,
    ):
        with pytest.raises(IntegrityError):
            Appointment.objects.create(
                **self._base(customer, specialist, marketplace_service),
            )

    def test_both_set_rejected(
        self, customer, specialist, marketplace_service, salon,
    ):
        with pytest.raises(IntegrityError):
            Appointment.objects.create(
                **self._base(customer, specialist, marketplace_service),
                service=marketplace_service,
                salon_service=salon,
            )

    def test_marketplace_only_valid(
        self, customer, specialist, marketplace_service,
    ):
        appt = Appointment.objects.create(
            **self._base(customer, specialist, marketplace_service),
            service=marketplace_service,
        )
        assert appt.pk is not None

    def test_salon_only_valid(self, customer, specialist, marketplace_service, salon):
        appt = Appointment.objects.create(
            **self._base(customer, specialist, marketplace_service),
            salon_service=salon,
        )
        assert appt.pk is not None


# ---------------------------------------------------------------------------
# Error shapes (unchanged — no existence leak)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSalonErrorShapes:
    def test_no_link_422(self, customer, specialist, salon):
        r = _api().post(CREATE_URL, {
            "client_id": str(customer.id),
            "specialist_id": str(specialist.id),
            "service_id": str(salon.id),
            "start_datetime": _future(3).isoformat(),
        }, format="json")
        assert r.status_code == 422
        assert r.data["error"]["code"] == "SERVICE_NOT_ACTIVE"
        assert Appointment.objects.count() == 0

    def test_inactive_link_422(self, customer, specialist, salon_link):
        salon_link.is_active = False
        salon_link.save()
        r = _api().post(CREATE_URL, {
            "client_id": str(customer.id),
            "specialist_id": str(specialist.id),
            "service_id": str(salon_link.salon_service_id),
            "start_datetime": _future(3).isoformat(),
        }, format="json")
        assert r.status_code == 422
        assert r.data["error"]["code"] == "SERVICE_NOT_ACTIVE"


# ---------------------------------------------------------------------------
# Readers on snapshots (option A)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSalonReaders:
    def _salon_booking(self, customer, specialist, salon_link):
        result = CreateBookingService().execute(
            _dto(customer, specialist, salon_link.salon_service_id),
        )
        return Appointment.objects.get(id=result.booking_id)

    def test_records_api_reads_salon_from_snapshots(
        self, customer, specialist, salon_link,
    ):
        appt = self._salon_booking(customer, specialist, salon_link)
        r = _api().get(RECORDS_URL)
        assert r.status_code == 200, r.data
        items = r.json()["data"]["items"]
        mine = [x for x in items if x["id"] == str(appt.id)]
        assert mine, "salon booking must appear in records"
        svc = mine[0]["service"]
        assert svc["id"] == str(salon_link.salon_service_id)
        assert svc["name"] == salon_link.salon_service.name
        assert svc["duration_minutes"] == 60

    def test_receipt_description_uses_snapshot(
        self, customer, specialist, salon_link,
    ):
        appt = self._salon_booking(customer, specialist, salon_link)
        receipt = build_appointment_receipt(appt, appt.price)
        assert receipt["items"][0]["description"] == (
            salon_link.salon_service.name
        )
        assert receipt["items"][0]["amount"]["value"] == "2000.00"

    def test_internal_payment_create_on_salon_booking(
        self, customer, specialist, salon_link,
    ):
        """C7.1 on a salon booking: amount from the snapshot, description
        from the snapshot name (no AttributeError on service=None)."""
        from unittest.mock import MagicMock, patch
        appt = self._salon_booking(customer, specialist, salon_link)
        assert appt.service_id is None
        svc = MagicMock()
        svc.create_payment.return_value = {
            "provider_payment_id": "yk_salon_1",
            "confirmation_url": "https://yookassa.ru/confirm/salon",
            "status": "pending",
            "platform_fee": Decimal("90.00"),
            "specialist_income": Decimal("1910.00"),
        }
        with patch("payments.views._get_yookassa", return_value=svc):
            r = _api().post(
                f"/api/v1/internal/appointments/{appt.id}/payment/",
                {"client_id": str(customer.id),
                 "return_url": "https://miniapp.example/done"},
                format="json",
            )
        assert r.status_code == 200, r.data
        call = svc.create_payment.call_args
        assert call.kwargs["amount"] == Decimal("2000.00")  # snapshot
        assert salon_link.salon_service.name in call.kwargs["description"]
