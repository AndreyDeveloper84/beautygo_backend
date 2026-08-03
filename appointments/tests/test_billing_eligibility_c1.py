"""C1 Billing Eligibility (PILOT_CONTRACTS §2) — W1 consumer side.

The billing module is W2's deliverable and may be ABSENT in this repo
state — the adapter must fail-open then (C1). Tests inject a fake
``billing.services`` into sys.modules to pin the contract:

- ok=False ("SUBSCRIPTION_PAST_DUE") blocks ONLY new booking creation:
  internal API → 409 SUBSCRIPTION_PAST_DUE; client API → 409 UNAVAILABLE
  with a neutral message (debt never disclosed);
- ok=True / module missing / technical error → booking proceeds
  (fail-open + Sentry telemetry, not asserted here);
- idempotent replay of an existing booking is NOT a new creation →
  never blocked;
- cancel / complete are not gated.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-c1"
EXTERNAL_USER_ID = "bot:c1"
INTERNAL_CREATE_URL = "/api/v1/internal/appointments/"
CLIENT_CREATE_URL = "/api/v1/appointments/"


@dataclass(frozen=True)
class FakeEligibilityResult:
    ok: bool
    reason: str | None = None


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79991001001", is_proxy=True,
    )


@pytest.fixture
def specialist(db):
    u = User.objects.create_user(
        username="c1_spec", password="x", role="specialist",
        phone="+79991001002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "C1 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="C1 Cat", slug="c1-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist, category=category, name="C1 Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
    )


def _future_iso(hours: int = 3) -> str:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0).isoformat()


def _fake_billing(monkeypatch, fn):
    """Install a fake billing.services module exposing fn as
    can_accept_booking. A PEP-562 __getattr__ returns a dummy for any
    OTHER name — billing/internal_api.py (P3 urls) does its own
    ``from billing.services import build_billing_status`` when the
    URLconf loads, and without a fallback the fake breaks unrelated
    imports depending on test order."""
    fake = types.ModuleType("billing.services")
    fake.can_accept_booking = fn
    fake.__getattr__ = lambda name: (lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "billing.services", fake)
    return fake


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


@pytest.mark.django_db
class TestEligibilityRefusal:
    def test_internal_create_409_subscription_past_due(
        self, monkeypatch, customer, specialist, service,
    ):
        _fake_billing(monkeypatch, lambda **kw: FakeEligibilityResult(
            ok=False, reason="SUBSCRIPTION_PAST_DUE",
        ))
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 409
        assert r.data["error"]["code"] == "SUBSCRIPTION_PAST_DUE"
        assert Appointment.objects.count() == 0

    def test_client_create_generic_unavailable_no_debt_disclosure(
        self, monkeypatch, customer, specialist, service,
    ):
        _fake_billing(monkeypatch, lambda **kw: FakeEligibilityResult(
            ok=False, reason="SUBSCRIPTION_PAST_DUE",
        ))
        r = _client_api(customer).post(CLIENT_CREATE_URL, {
            "specialist_id": str(specialist.id),
            "service_id": str(service.id),
            "start_datetime": _future_iso(3),
        }, format="json")
        assert r.status_code == 409
        assert r.data["error"]["code"] == "UNAVAILABLE"
        # Privacy (C1 §2): no debt vocabulary leaks to the customer.
        payload = str(r.data).lower()
        assert "past_due" not in payload
        assert "долг" not in payload and "задолжен" not in payload

    def test_cancel_not_blocked_by_past_due(
        self, monkeypatch, customer, specialist, service,
    ):
        """C1: only NEW creation is gated — existing bookings still
        cancel/complete normally."""
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201
        booking_id = r.data["data"]["id"]
        _fake_billing(monkeypatch, lambda **kw: FakeEligibilityResult(
            ok=False, reason="SUBSCRIPTION_PAST_DUE",
        ))
        c = _internal_api()
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = "c1-cancel-not-blocked"
        r = c.post(
            f"/api/v1/internal/appointments/{booking_id}/cancel/",
            {"reason": "client changed mind"}, format="json",
        )
        assert r.status_code == 200, r.data


@pytest.mark.django_db
class TestEligibilityAllows:
    def test_ok_true_creates(self, monkeypatch, customer, specialist, service):
        _fake_billing(monkeypatch, lambda **kw: FakeEligibilityResult(ok=True))
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data

    def test_missing_billing_module_fails_open(
        self, monkeypatch, customer, specialist, service,
    ):
        """billing.services unavailable (import error) → booking
        proceeds (C1 fail-open). Simulated by pointing the adapter at a
        nonexistent module — billing/ itself now lives in the repo."""
        monkeypatch.setattr(
            "appointments.application.services.billing_eligibility"
            "._BILLING_MODULE",
            "billing.nonexistent_module",
        )
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data

    def test_technical_error_fails_open(
        self, monkeypatch, customer, specialist, service,
    ):
        def _boom(**kw):
            raise RuntimeError("db down")
        _fake_billing(monkeypatch, _boom)
        r = _internal_api().post(
            INTERNAL_CREATE_URL, _body(customer, specialist, service),
            format="json",
        )
        assert r.status_code == 201, r.data

    def test_idempotent_replay_not_blocked(
        self, monkeypatch, customer, specialist, service,
    ):
        """A retried create of an EXISTING booking is not a new
        creation — eligibility must not refuse it."""
        key = str(uuid4())
        c = _internal_api()
        c.defaults["HTTP_X_IDEMPOTENCY_KEY"] = key
        body = _body(customer, specialist, service)
        r1 = c.post(INTERNAL_CREATE_URL, body, format="json")
        assert r1.status_code == 201
        _fake_billing(monkeypatch, lambda **kw: FakeEligibilityResult(
            ok=False, reason="SUBSCRIPTION_PAST_DUE",
        ))
        r2 = c.post(INTERNAL_CREATE_URL, body, format="json")
        assert r2.status_code == 201
        assert r2.data["data"]["id"] == r1.data["data"]["id"]
