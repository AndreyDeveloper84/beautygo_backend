"""Tests for Records endpoints (task #97) — W1 customer-records-flow.

Three endpoints under /api/v1/internal/me/bookings/:
- GET /             — list with section=upcoming|history, cursor paging
- GET /{id}/        — full detail
- POST /{id}/repeat-intent/ — prefill data

Identity bridging: Bearer + X-External-User-ID (per memory
project_identity_bridging_pattern). Re-uses IsBotServiceWithVerifiedClient
permission from PR #158 — auth tests there cover the bearer/header
boundary; here we focus on the *contract* (section filter, cursor
pagination, multi-tenant scope, derived_status taxonomy).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

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
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


VALID_TOKEN = "test-ayla-internal-token-records"
LIST_URL = "/api/v1/internal/me/bookings/"


def _detail_url(booking_id) -> str:
    return f"/api/v1/internal/me/bookings/{booking_id}/"


def _repeat_url(booking_id) -> str:
    return f"/api/v1/internal/me/bookings/{booking_id}/repeat-intent/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id():
    return "bot:records"


@pytest.fixture
def customer(db, external_user_id):
    # resolve_external_user looks up by username.
    # Phone in +7-700XXXXX block so no collision with specialist
    # fixtures (which use +7-99930XXXXX).
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79997000001", is_proxy=True,
    )


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="rec-a", name="Records Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="rec-b", name="Records Tenant B")


def _make_specialist(tenant, *, suffix: str, name: str):
    user = User.objects.create_user(
        username=f"rec_spec_{suffix}", password="x", role="specialist",
        phone=f"+799930{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.tenant = tenant
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.save()
    return profile


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="RecCat", slug="rec-cat")


def _make_service(specialist_profile, category):
    return Service.objects.create(
        specialist=specialist_profile,
        category=category,
        name="Rec Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _book(*, customer, specialist_profile, service, start_at):
    dto = CreateBookingDTO(
        client_id=customer.id,
        specialist_id=specialist_profile.id,
        service_id=service.id,
        start_at=start_at,
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist_profile, service,
        target_interval=TimeInterval(
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        ),
    )
    # Variant E might not have granted TUR if request_tenant_id was None —
    # add one explicitly so the customer counts as a tenant member.
    TenantUserRelationship.objects.get_or_create(
        user=customer, tenant=specialist_profile.tenant,
        defaults={
            "role": TenantUserRelationship.Role.CUSTOMER,
            "is_active": True,
        },
    )
    return appt


def _future(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _api(
    *, bearer: str | None = VALID_TOKEN, external_user_id: str = "bot:records",
) -> APIClient:
    c = APIClient()
    # internal/me/ is in middleware exclusion (no X-App-Type required)
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


# ---------------------------------------------------------------------------
# Auth boundary — sanity, deeper coverage lives in PR #158 tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordsAuth:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_bearer_denied(self, customer):
        r = _api(bearer=None).get(LIST_URL)
        assert r.status_code == 403

    def test_wrong_bearer_denied(self, customer):
        r = _api(bearer="wrong").get(LIST_URL)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Section filter — upcoming vs history
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSectionFilter:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_section_upcoming_returns_only_future_active(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00001", name="A")
        svc = _make_service(spec, category)
        future = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        # Past booking — backdate via DB to bypass booking-window
        # validation.
        past = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(6),
        )
        Appointment.objects.filter(id=past.id).update(
            start_datetime=datetime.now(tz=timezone.utc) - timedelta(hours=2),
            end_datetime=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        )

        r = _api().get(LIST_URL + "?section=upcoming")
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        ids = {it["id"] for it in items}
        assert str(future.id) in ids
        assert str(past.id) not in ids

    def test_section_history_returns_terminal_or_past(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00002", name="B")
        svc = _make_service(spec, category)
        completed = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=completed.id).update(
            status=Appointment.Status.COMPLETED,
        )
        future = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(5),
        )

        r = _api().get(LIST_URL + "?section=history")
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        ids = {it["id"] for it in items}
        assert str(completed.id) in ids
        assert str(future.id) not in ids

    def test_invalid_section_400(self, customer):
        r = _api().get(LIST_URL + "?section=nonsense")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Multi-tenant: customer sees bookings across all their tenants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMultiTenantScope:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_returns_bookings_from_both_tenants(
        self, customer, tenant_a, tenant_b, category,
    ):
        spec_a = _make_specialist(tenant_a, suffix="00010", name="MA")
        spec_b = _make_specialist(tenant_b, suffix="00011", name="MB")
        cat_b = ServiceCategory.objects.create(slug="rec-cat-b", name="B")
        svc_a = _make_service(spec_a, category)
        svc_b = _make_service(spec_b, cat_b)
        b_a = _book(
            customer=customer, specialist_profile=spec_a, service=svc_a,
            start_at=_future(3),
        )
        b_b = _book(
            customer=customer, specialist_profile=spec_b, service=svc_b,
            start_at=_future(5),
        )

        r = _api().get(LIST_URL + "?section=upcoming")
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        ids = {it["id"] for it in items}
        assert str(b_a.id) in ids
        assert str(b_b.id) in ids
        # Tenant info exposed per-item for UI grouping.
        tenants_by_id = {it["id"]: it["tenant"] for it in items}
        assert tenants_by_id[str(b_a.id)]["slug"] == "rec-a"
        assert tenants_by_id[str(b_b.id)]["slug"] == "rec-b"

    def test_other_customers_bookings_not_visible(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00012", name="C")
        svc = _make_service(spec, category)
        # Another customer with a booking.
        other = User.objects.create_user(
            username="rec_other_customer", password="x", role="client",
            phone="+79993099999", is_proxy=False,
        )
        other_appt = _book(
            customer=other, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )

        r = _api().get(LIST_URL + "?section=upcoming")
        items = r.json()["data"]["items"]
        ids = {it["id"] for it in items}
        assert str(other_appt.id) not in ids


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCursorPagination:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_limit_caps_page_size_and_emits_next_cursor(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00020", name="P")
        svc = _make_service(spec, category)
        # 3 future bookings at distinct slots
        booked = []
        for hours in (3, 5, 7):
            booked.append(_book(
                customer=customer, specialist_profile=spec, service=svc,
                start_at=_future(hours),
            ))

        r = _api().get(LIST_URL, {"section": "upcoming", "limit": "2"})
        body = r.json()["data"]
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        # APIClient.get with dict QueryDict-encodes the cursor, so the
        # "+" in the tz offset is properly URL-encoded as %2B instead
        # of decaying to a space.
        r2 = _api().get(LIST_URL, {
            "section": "upcoming",
            "limit": "2",
            "cursor": body["next_cursor"],
        })
        body2 = r2.json()["data"]
        assert len(body2["items"]) == 1
        assert body2["next_cursor"] is None
        # Combined pages == full set, no duplicates.
        page1_ids = {it["id"] for it in body["items"]}
        page2_ids = {it["id"] for it in body2["items"]}
        assert page1_ids.isdisjoint(page2_ids)
        assert page1_ids | page2_ids == {str(b.id) for b in booked}

    def test_invalid_cursor_400(self, customer):
        r = _api().get(LIST_URL + "?section=upcoming&cursor=not-a-date")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Derived status taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDerivedStatusTaxonomy:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_confirmed_status(self, customer, tenant_a, category):
        spec = _make_specialist(tenant_a, suffix="00030", name="DS")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.CONFIRMED,
        )
        r = _api().get(_detail_url(appt.id))
        assert r.status_code == 200
        assert r.json()["data"]["derived_status"] == "confirmed"

    def test_no_fault_provider_cancelled(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00031", name="DS2")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.CANCELLED,
            cancellation_reason="specialist_departure",
        )
        r = _api().get(_detail_url(appt.id))
        assert r.json()["data"]["derived_status"] == "no_fault_provider_cancelled"

    def test_customer_cancelled_with_refund(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00032", name="DS3")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.CANCELLED,
            cancelled_by=customer,
            cancellation_reason="changed_my_mind",
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            refunded_amount=Decimal("1500.00"),
            status=Payment.Status.REFUNDED,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-cust-refund",
        )
        r = _api().get(_detail_url(appt.id))
        body = r.json()["data"]
        assert body["derived_status"] == "customer_cancelled_with_refund"
        assert body["cancelled_by_client"] is True

    def test_partial_refund(self, customer, tenant_a, category):
        spec = _make_specialist(tenant_a, suffix="00033", name="DS4")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.COMPLETED,
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            refunded_amount=Decimal("500.00"),
            status=Payment.Status.PARTIALLY_REFUNDED,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-partial",
        )
        r = _api().get(_detail_url(appt.id))
        assert r.json()["data"]["derived_status"] == "partial_refund"


# ---------------------------------------------------------------------------
# Detail + info-hiding
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDetailEndpoint:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_other_customers_booking_returns_404(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00040", name="D")
        svc = _make_service(spec, category)
        other = User.objects.create_user(
            username="rec_other_d", password="x", role="client",
            phone="+79993300040",
        )
        other_appt = _book(
            customer=other, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        r = _api().get(_detail_url(other_appt.id))
        assert r.status_code == 404

    def test_unknown_booking_returns_404(self, customer):
        r = _api().get(_detail_url(uuid4()))
        assert r.status_code == 404

    def test_detail_includes_payments_and_notes(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00041", name="DT")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        Appointment.objects.filter(id=appt.id).update(
            notes="Принести браслет",
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            status=Payment.Status.PAID,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-paid",
        )
        r = _api().get(_detail_url(appt.id))
        body = r.json()["data"]
        assert body["notes"] == "Принести браслет"
        # The booking fixture's CreateBookingService._execute_atomic
        # auto-creates a Payment row, and the test added a second one
        # explicitly. Detail returns BOTH ordered by created_at desc;
        # the most-recent (paid) is the one this test cares about.
        assert len(body["payments"]) >= 1
        paid_payments = [p for p in body["payments"] if p["status"] == "paid"]
        assert len(paid_payments) == 1


# ---------------------------------------------------------------------------
# Repeat-intent
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRepeatIntent:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_returns_prefill_data(self, customer, tenant_a, category):
        spec = _make_specialist(tenant_a, suffix="00050", name="RI")
        svc = _make_service(spec, category)
        appt = _book(
            customer=customer, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )

        r = _api().post(_repeat_url(appt.id))
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["service_id"] == str(svc.id)
        assert body["specialist_id"] == str(spec.id)
        assert Decimal(body["last_price"]) == appt.price
        # MVP: suggested_slots empty.
        assert body["suggested_slots"] == []

    def test_repeat_intent_other_user_404(
        self, customer, tenant_a, category,
    ):
        spec = _make_specialist(tenant_a, suffix="00051", name="RI2")
        svc = _make_service(spec, category)
        other = User.objects.create_user(
            username="rec_other_ri", password="x", role="client",
            phone="+79993300051",
        )
        other_appt = _book(
            customer=other, specialist_profile=spec, service=svc,
            start_at=_future(3),
        )
        r = _api().post(_repeat_url(other_appt.id))
        assert r.status_code == 404
