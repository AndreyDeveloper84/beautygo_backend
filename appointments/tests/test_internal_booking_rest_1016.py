"""Internal Bearer booking REST — create / cancel / reschedule (#1016 S2).

The nationwide Ayla bot drives bookings through these endpoints on
behalf of a verified customer (Bearer + X-External-User-ID). Pins:
- auth boundary (missing / wrong bearer, missing external id → 403);
- create on behalf of the resolved customer → 201 + appointment owned
  by that customer;
- defense-in-depth client_id cross-check (body must echo resolved id);
- X-Idempotency-Key dedupes a retried create;
- grant-on-first-booking fires through this channel too (#1014);
- cancel / reschedule on the resolved customer's booking;
- no X-App-Type and no X-Tenant header required on the internal tree.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


VALID_TOKEN = "test-ayla-internal-token-1016"
EXTERNAL_USER_ID = "bot:1016"
CREATE_URL = "/api/v1/internal/appointments/"


def _cancel_url(booking_id) -> str:
    return f"/api/v1/internal/appointments/{booking_id}/cancel/"


def _reschedule_url(booking_id) -> str:
    return f"/api/v1/internal/appointments/{booking_id}/reschedule/"


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="i1016-t", name="Internal 1016 Tenant")


@pytest.fixture
def customer(db):
    # resolve_external_user resolves bot:1016 by username — pre-create so
    # the resolved User is deterministic across the test.
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79991016000", is_proxy=True,
    )


@pytest.fixture
def specialist(db, tenant):
    u = User.objects.create_user(
        username="i1016_spec", password="x", role="specialist",
        phone="+79991016001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Internal Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="I1016 Cat", slug="i1016-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="Internal Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _api(
    *, bearer: str | None = VALID_TOKEN,
    external_user_id: str | None = EXTERNAL_USER_ID,
) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _create_body(customer, specialist, service, hours: int = 3) -> dict:
    return {
        "client_id": str(customer.id),
        "specialist_id": str(specialist.id),
        "service_id": str(service.id),
        "start_datetime": _future_iso(hours),
    }


@pytest.mark.django_db
class TestInternalCreateAuth:
    def test_missing_bearer_denied(self, customer, specialist, service):
        r = _api(bearer=None).post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 403

    def test_wrong_bearer_denied(self, customer, specialist, service):
        r = _api(bearer="nope").post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 403

    def test_missing_external_user_id_denied(
        self, customer, specialist, service,
    ):
        r = _api(external_user_id=None).post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 403

    def test_empty_token_setting_fails_closed(
        self, settings, customer, specialist, service,
    ):
        settings.AYLA_INTERNAL_API_TOKEN = ""
        r = _api().post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 403


@pytest.mark.django_db
class TestInternalCreate:
    def test_create_on_behalf_succeeds(self, customer, specialist, service):
        r = _api().post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get()
        assert appt.client_id == customer.id
        assert appt.specialist_id == specialist.id
        # No X-App-Type / X-Tenant header was sent — internal tree is
        # exempt from both middlewares.

    def test_create_grants_tur_first_booking(
        self, customer, specialist, service, tenant,
    ):
        """Grant-on-first-booking (#1014) fires through the bot channel."""
        assert not TenantUserRelationship.objects.filter(
            user=customer, tenant=tenant, is_active=True,
        ).exists()
        r = _api().post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data
        assert TenantUserRelationship.objects.filter(
            user=customer, tenant=tenant, is_active=True,
        ).count() == 1

    def test_multi_segment_external_user_id_resolves(
        self, specialist, service,
    ):
        """Channel-scoped X-External-User-ID (bot:{channel}:{id}, #1016 §2)
        resolves to a proxy actor and the booking write succeeds."""
        from users.services import resolve_external_user
        actor = resolve_external_user("bot:telegram:90001")
        body = {
            "client_id": str(actor.id),
            "specialist_id": str(specialist.id),
            "service_id": str(service.id),
            "start_datetime": _future_iso(3),
        }
        r = _api(external_user_id="bot:telegram:90001").post(
            CREATE_URL, body, format="json",
        )
        assert r.status_code == 201, r.data
        assert Appointment.objects.get().client_id == actor.id

    def test_client_id_mismatch_forbidden(
        self, customer, specialist, service,
    ):
        body = _create_body(customer, specialist, service)
        body["client_id"] = str(uuid4())  # not the resolved actor
        r = _api().post(CREATE_URL, body, format="json")
        assert r.status_code == 403
        assert r.data["error"]["code"] == "CLIENT_MISMATCH"
        assert Appointment.objects.count() == 0

    def test_idempotency_key_dedupes_retry(
        self, customer, specialist, service,
    ):
        body = _create_body(customer, specialist, service)
        key = str(uuid4())
        c = _api()
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = key
        r1 = c.post(CREATE_URL, body, format="json")
        r2 = c.post(CREATE_URL, body, format="json")
        assert r1.status_code == 201
        assert r2.status_code == 201
        # Same idempotency key → one appointment, returned twice.
        assert Appointment.objects.count() == 1
        assert r1.data["data"]["id"] == r2.data["data"]["id"]

    def test_slot_conflict_returns_409(self, customer, specialist, service):
        body = _create_body(customer, specialist, service, hours=4)
        r1 = _api().post(CREATE_URL, body, format="json")
        assert r1.status_code == 201
        # A different idempotency key, same slot → conflict.
        r2 = _api().post(CREATE_URL, body, format="json")
        assert r2.status_code == 409


@pytest.mark.django_db
class TestInternalCancelReschedule:
    def _book(self, customer, specialist, service, hours=3):
        r = _api().post(
            CREATE_URL, _create_body(customer, specialist, service, hours),
            format="json",
        )
        assert r.status_code == 201, r.data
        return r.data["data"]["id"]

    def test_cancel_on_behalf(self, customer, specialist, service):
        booking_id = self._book(customer, specialist, service)
        r = _api().post(
            _cancel_url(booking_id), {"reason": "client changed mind"},
            format="json",
        )
        assert r.status_code == 200, r.data
        appt = Appointment.objects.get(id=booking_id)
        assert appt.status == Appointment.Status.CANCELLED

    def test_cancel_other_customers_booking_404(
        self, customer, specialist, service,
    ):
        booking_id = self._book(customer, specialist, service)
        # A different external identity resolves to a different proxy
        # user who does not own the booking → info-hidden 404.
        r = _api(external_user_id="bot:stranger").post(
            _cancel_url(booking_id), {"reason": "not mine"}, format="json",
        )
        assert r.status_code == 404

    def test_reschedule_on_behalf(self, customer, specialist, service):
        # Book well outside the 4h reschedule-notice window.
        booking_id = self._book(customer, specialist, service, hours=48)
        # Reschedule policy only allows a CONFIRMED booking; the online
        # payment hold would normally move it there. Simulate that here.
        Appointment.objects.filter(id=booking_id).update(
            status=Appointment.Status.CONFIRMED,
        )
        new_start = _future_iso(72)
        r = _api().post(
            _reschedule_url(booking_id), {"new_start_datetime": new_start},
            format="json",
        )
        assert r.status_code == 200, r.data
        appt = Appointment.objects.get(id=booking_id)
        assert appt.start_datetime.isoformat() == new_start


@pytest.mark.django_db
class TestInternalCreateNoPrepayment:
    """D6 — the bot can book a customer WITHOUT prepayment: the booking
    lands directly in CONFIRMED (no Payment row) and booking.confirmed
    is emitted (R1 — W3 schedules the T-24h reminder off it). The
    online-payment path (default) is preserved byte-for-byte."""

    def test_create_without_prepayment_confirms(
        self, customer, specialist, service,
    ):
        body = _create_body(customer, specialist, service)
        body["payment_required"] = False
        r = _api().post(CREATE_URL, body, format="json")
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get()
        assert appt.status == Appointment.Status.CONFIRMED
        assert appt.payments.count() == 0
        topics = set(OutboxEvent.objects.values_list("topic", flat=True))
        assert OutboxEvent.Topic.BOOKING_CREATED in topics
        assert OutboxEvent.Topic.BOOKING_CONFIRMED in topics

    def test_default_path_still_awaits_payment(
        self, customer, specialist, service,
    ):
        r = _api().post(
            CREATE_URL, _create_body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data
        appt = Appointment.objects.get()
        assert appt.status == Appointment.Status.AWAITING_PAYMENT
        assert appt.payments.filter(status="pending").count() == 1
        # No hold yet → not confirmed → no booking.confirmed.
        topics = set(OutboxEvent.objects.values_list("topic", flat=True))
        assert OutboxEvent.Topic.BOOKING_CONFIRMED not in topics

    def test_no_prepayment_booking_is_cancellable(
        self, customer, specialist, service,
    ):
        body = _create_body(customer, specialist, service)
        body["payment_required"] = False
        r = _api().post(CREATE_URL, body, format="json")
        assert r.status_code == 201, r.data
        booking_id = r.data["data"]["id"]
        r = _api().post(
            _cancel_url(booking_id), {"reason": "client changed mind"},
            format="json",
        )
        assert r.status_code == 200, r.data
        assert Appointment.objects.get(id=booking_id).status == (
            Appointment.Status.CANCELLED
        )
