"""E2E-BOT-02B — identity binding on the records surface.

Covers the full deterministic path the bot's ``show_my_bookings`` relies
on, at the boundary where E2E-BOT-02 failed before the fix:

    external id (X-External-User-ID)
    → resolve_external_user()
    → bound real User (Phase C linked_user)
    → Appointment.objects.filter(client=request.user)

Scenarios:

* Synthetic customer with 3 appointments + binding → the records list
  returns exactly those 3 (was: 200 with ``[]`` from the isolated
  proxy account).
* Regression — NO binding → controlled empty result: 200 with
  ``items == []``, never a fallback to another customer's data.
* No cross-customer leakage through a binding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from users.services import bind_external_identity, resolve_external_user


VALID_TOKEN = "test-ayla-internal-token-binding"
LIST_URL = "/api/v1/internal/me/bookings/"
EXTERNAL_ID = "bot:max:e2e-binding"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _api(external_user_id: str = EXTERNAL_ID) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


@pytest.fixture
def real_customer(db):
    # The "e2e-wave1-customer" analogue: a REAL account owning the
    # appointments. Crucially its username is NOT the external id —
    # without the binding there is no path from one to the other.
    return User.objects.create_user(
        username="e2e-binding-customer", password="x", role="client",
        phone="+79996660001", is_proxy=False, is_verified=True,
    )


@pytest.fixture
def other_customer(db):
    return User.objects.create_user(
        username="e2e-binding-other", password="x", role="client",
        phone="+79996660002", is_proxy=False, is_verified=True,
    )


@pytest.fixture
def booking_context(db):
    tenant = Tenant.objects.create(slug="bind-tenant", name="Binding Tenant")
    spec_user = User.objects.create_user(
        username="bind_spec", password="x", role="specialist",
        phone="+79996660003",
    )
    specialist = SpecialistProfile.objects.get(user=spec_user)
    specialist.display_name = "Binding Master"
    specialist.tenant = tenant
    specialist.status = SpecialistProfile.ProfileStatus.ACTIVE
    specialist.save()
    category = ServiceCategory.objects.create(
        name="BindCat", slug="bind-cat", tenant=tenant,
    )
    service = Service.objects.create(
        specialist=specialist, category=category, tenant=tenant,
        name="Binding Massage", price=Decimal("2500.00"),
        duration_minutes=60, is_active=True, buffer_after_minutes=0,
    )
    return {"tenant": tenant, "specialist": specialist, "service": service}


def _book(customer, ctx, *, starts_at: datetime, key: str) -> Appointment:
    return Appointment.objects.create(
        client=customer,
        specialist=ctx["specialist"],
        tenant=ctx["tenant"],
        service=ctx["service"],
        salon_service=None,
        start_datetime=starts_at,
        end_datetime=starts_at + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED,
        version=1,
        price=Decimal("2500.00"),
        snapshot_service_name=ctx["service"].name,
        snapshot_duration_minutes=60,
        snapshot_price=Decimal("2500.00"),
        snapshot_timezone="Europe/Moscow",
        idempotency_key=key,
    )


@pytest.fixture
def three_appointments(db, real_customer, booking_context):
    base = (datetime.now(tz=timezone.utc) + timedelta(hours=48)).replace(
        second=0, microsecond=0,
    )
    return [
        _book(real_customer, booking_context,
              starts_at=base + timedelta(hours=24 * i),
              key=f"e2e-binding-{i}")
        for i in range(3)
    ]


@pytest.mark.django_db
class TestBoundIdentityRecords:
    """Synthetic user → binding → 3 Appointment → backend returns all 3."""

    def test_bound_identity_sees_all_three(self, real_customer,
                                           three_appointments):
        bind_external_identity(EXTERNAL_ID, real_customer.pk)

        r = _api().get(LIST_URL, {"section": "upcoming"})

        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert len(items) == 3
        expected = {str(a.pk) for a in three_appointments}
        assert {item["id"] for item in items} == expected

    def test_bound_identity_detail_works(self, real_customer,
                                         three_appointments):
        bind_external_identity(EXTERNAL_ID, real_customer.pk)
        booking_id = three_appointments[0].pk

        r = _api().get(f"{LIST_URL}{booking_id}/")

        assert r.status_code == 200
        assert r.json()["data"]["id"] == str(booking_id)

    def test_binding_leaks_nothing_from_other_customers(
        self, real_customer, other_customer, three_appointments,
        booking_context,
    ):
        foreign = _book(
            other_customer, booking_context,
            starts_at=(datetime.now(tz=timezone.utc) + timedelta(hours=96))
            .replace(second=0, microsecond=0),
            key="e2e-binding-foreign",
        )
        bind_external_identity(EXTERNAL_ID, real_customer.pk)

        r = _api().get(LIST_URL, {"section": "upcoming"})

        assert r.status_code == 200
        ids = {item["id"] for item in r.json()["data"]["items"]}
        assert str(foreign.pk) not in ids
        assert len(ids) == 3


@pytest.mark.django_db
class TestUnboundIdentityRegression:
    """No binding → controlled empty result, never a random account."""

    def test_unbound_external_id_gets_empty_list(self, real_customer,
                                                 three_appointments):
        # No bind_external_identity call — the proxy stays isolated.
        r = _api().get(LIST_URL, {"section": "upcoming"})

        assert r.status_code == 200
        assert r.json()["data"]["items"] == []
        assert r.json()["data"]["next_cursor"] is None

    def test_unbound_resolution_creates_isolated_proxy(self, real_customer,
                                                       three_appointments):
        resolved = resolve_external_user(EXTERNAL_ID)
        assert resolved.is_proxy is True
        assert resolved.pk != real_customer.pk
        assert resolved.linked_user_id is None
