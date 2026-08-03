"""Joint W1×W2 invariant tests (P6) — REAL billing module, no fakes.

C1 (PILOT_CONTRACTS §2 + AMD-005):
- past_due blocks NEW booking creation: internal → 409
  SUBSCRIPTION_PAST_DUE; client → 409 generic UNAVAILABLE;
- a past_due SALON subscription blocks ALL masters of the tenant, even
  ones with a healthy personal subscription;
- fail-open when no billing account exists;
- only NEW creation is gated (cancel works under past_due);
- the W1 adapter keys the account by Ayla User UUID (AMD-005).

AYLA-DEC-0010 (AMD-009/AMD-011) — fee accrues exactly once, and only
for bookings WITHOUT online payment:
- Payment in {authorized, paid, refunded, partially_refunded} → NO fee;
- Payment in {failed, pending} → fee accrues (abandoned attempts);
- repeated accrual/handler delivery → still exactly one BookingFee.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from billing.models import BookingFee, SpecialistSubscription, TariffPlan
from billing.services import accrue_booking_fee
from payments.models import Payment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-jw2"
EXTERNAL_USER_ID = "bot:jw2"
INTERNAL_CREATE_URL = "/api/v1/internal/appointments/"
CLIENT_CREATE_URL = "/api/v1/appointments/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79992002001", is_proxy=True,
    )


def _make_specialist(username: str, phone: str, tenant=None) -> SpecialistProfile:
    u = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = username
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="JW2 Cat", slug="jw2-cat")


def _make_service(specialist, category, name="JW2 Service") -> Service:
    return Service.objects.create(
        specialist=specialist, category=category, name=name,
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
    )


def _subscription(user, *, status, tenant=None, tariff_code="solo"):
    return SpecialistSubscription.objects.create(
        user=user, tenant=tenant,
        tariff=TariffPlan.objects.get(code=tariff_code),
        status=status,
    )


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


def _internal_api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _client_api(user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


def _body(customer, specialist, service) -> dict:
    return {
        "client_id": str(customer.id),
        "specialist_id": str(specialist.id),
        "service_id": str(service.id),
        "start_datetime": _future_iso(3),
    }


# ---------------------------------------------------------------------------
# C1 — real billing module through the W1 adapter
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestJointC1:
    def test_personal_past_due_blocks_internal_create(
        self, customer, category,
    ):
        spec = _make_specialist("jw2_pastdue", "+79992002002")
        _make_service(spec, category)
        _subscription(
            spec.user, status=SpecialistSubscription.Status.PAST_DUE,
        )
        service = Service.objects.get(specialist=spec)
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, spec, service),
            format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "SUBSCRIPTION_PAST_DUE"
        assert Appointment.objects.count() == 0

    def test_personal_past_due_client_gets_generic_unavailable(
        self, customer, category,
    ):
        spec = _make_specialist("jw2_pastdue2", "+79992002003")
        service = _make_service(spec, category)
        _subscription(
            spec.user, status=SpecialistSubscription.Status.PAST_DUE,
        )
        r = _client_api(customer).post(CLIENT_CREATE_URL, {
            "specialist_id": str(spec.id),
            "service_id": str(service.id),
            "start_datetime": _future_iso(3),
        }, format="json")
        assert r.status_code == 409
        assert r.data["error"]["code"] == "UNAVAILABLE"
        assert "past_due" not in str(r.data).lower()

    def test_salon_past_due_blocks_all_masters_of_tenant(
        self, customer, category,
    ):
        """C1 resolution: the SALON's past_due governs every master of
        the tenant — even one whose personal subscription is active."""
        tenant = Tenant.objects.create(slug="jw2-salon", name="JW2 Salon")
        master_blocked = _make_specialist("jw2_m1", "+79992002004", tenant)
        master_also_blocked = _make_specialist("jw2_m2", "+79992002005", tenant)
        service = _make_service(master_blocked, category)
        # Salon subscription past_due…
        _subscription(
            master_blocked.user, tenant=tenant,
            status=SpecialistSubscription.Status.PAST_DUE,
            tariff_code="salon",
        )
        # …and a healthy PERSONAL subscription on the other master —
        # the salon context still wins (C1: контекст конкретной записи).
        _subscription(
            master_also_blocked.user,
            status=SpecialistSubscription.Status.ACTIVE,
        )
        for spec in (master_blocked, master_also_blocked):
            r = _internal_api().post(
                INTERNAL_CREATE_URL, _body(customer, spec, service)
                if spec is master_blocked else {
                    "client_id": str(customer.id),
                    "specialist_id": str(spec.id),
                    "service_id": str(_make_service(spec, category, "S2").id),
                    "start_datetime": _future_iso(5),
                },
                format="json",
            )
            assert r.status_code == 409, (spec, r.data)
            assert r.data["error"]["code"] == "SUBSCRIPTION_PAST_DUE"
        assert Appointment.objects.count() == 0

    def test_no_billing_account_fails_open(self, customer, category):
        spec = _make_specialist("jw2_nosub", "+79992002006")
        service = _make_service(spec, category)
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, spec, service),
            format="json",
        )
        assert r.status_code == 201, r.data

    def test_active_subscription_allows(self, customer, category):
        spec = _make_specialist("jw2_active", "+79992002007")
        service = _make_service(spec, category)
        _subscription(spec.user, status=SpecialistSubscription.Status.ACTIVE)
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, spec, service),
            format="json",
        )
        assert r.status_code == 201, r.data

    def test_cancel_still_works_under_past_due(self, customer, category):
        spec = _make_specialist("jw2_cancel", "+79992002008")
        service = _make_service(spec, category)
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, spec, service),
            format="json",
        )
        assert r.status_code == 201
        booking_id = r.data["data"]["id"]
        _subscription(
            spec.user, status=SpecialistSubscription.Status.PAST_DUE,
        )
        c = _internal_api()
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = "jw2-cancel-past-due"
        r = c.post(
            f"/api/v1/internal/appointments/{booking_id}/cancel/",
            {"reason": "changed mind"}, format="json",
        )
        assert r.status_code == 200, r.data


# ---------------------------------------------------------------------------
# AYLA-DEC-0010 — single fee collection invariant (real billing accrual)
# ---------------------------------------------------------------------------

def _completed_appointment(customer, specialist, service) -> Appointment:
    now = datetime.now(tz=timezone.utc)
    return Appointment.objects.create(
        client=customer, specialist=specialist, service=service,
        start_datetime=now - timedelta(hours=3),
        end_datetime=now - timedelta(hours=2),
        status=Appointment.Status.COMPLETED,
        completed_at=now - timedelta(hours=2),
        price=service.price,
    )


def _payment(appointment, status) -> Payment:
    return Payment.objects.create(
        appointment=appointment, amount=appointment.price,
        status=status,
        specialist_income=appointment.price - Decimal("90.00"),
        platform_fee=Decimal("90.00"),
        provider="yookassa",
        provider_payment_id=f"yk_{status}_{appointment.id.hex[:8]}",
    )


@pytest.mark.django_db
class TestSingleFeeInvariant:
    @pytest.fixture
    def specialist(self, db):
        return _make_specialist("jw2_fee", "+79992002009")

    @pytest.fixture
    def service(self, specialist, category):
        return _make_service(specialist, category)

    @pytest.fixture
    def subscription(self, specialist):
        return _subscription(
            specialist.user, status=SpecialistSubscription.Status.ACTIVE,
        )

    @pytest.mark.parametrize("status", [
        Payment.Status.AUTHORIZED,
        Payment.Status.PAID,
        Payment.Status.REFUNDED,
        Payment.Status.PARTIALLY_REFUNDED,
    ])
    def test_online_paid_states_never_accrue_fee(
        self, customer, specialist, service, subscription, status,
    ):
        """Online-paid (incl. refunded — the money DID flow through the
        split, AMD-009) → no BookingFee."""
        appt = _completed_appointment(customer, specialist, service)
        _payment(appt, status)
        assert accrue_booking_fee(appt) is None
        assert BookingFee.objects.count() == 0

    @pytest.mark.parametrize("status", [
        Payment.Status.FAILED,
        Payment.Status.PENDING,
    ])
    def test_abandoned_payment_states_accrue_fee(
        self, customer, specialist, service, subscription, status,
    ):
        """failed/pending are abandoned attempts — the service was paid
        offline → the 90₽ fee accrues via provider billing."""
        appt = _completed_appointment(customer, specialist, service)
        _payment(appt, status)
        fee = accrue_booking_fee(appt)
        assert fee is not None
        assert fee.amount == Decimal("90.00")

    def test_no_payment_at_all_accrues_fee(
        self, customer, specialist, service, subscription,
    ):
        appt = _completed_appointment(customer, specialist, service)
        fee = accrue_booking_fee(appt)
        assert fee is not None
        assert fee.amount == Decimal("90.00")

    def test_fee_accrues_exactly_once(
        self, customer, specialist, service, subscription,
    ):
        """Direct call + a redelivery through the outbox handler — the
        OneToOne(appointment) guard keeps it to ONE row."""
        from billing.handlers import on_booking_completed
        from appointments.models import OutboxEvent

        appt = _completed_appointment(customer, specialist, service)
        fee1 = accrue_booking_fee(appt)
        fee2 = accrue_booking_fee(appt)  # direct repeat
        assert fee1.pk == fee2.pk

        event = OutboxEvent.objects.create(
            topic=OutboxEvent.Topic.BOOKING_COMPLETED,
            payload={"data": {"appointment_id": str(appt.id)}},
        )
        on_booking_completed(event)  # simulated redelivery
        on_booking_completed(event)
        assert BookingFee.objects.filter(appointment=appt).count() == 1
