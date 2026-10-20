"""DRF-2984 PR-C3 — the cabinet billing surface (contract §6).

Doors ``internal/billing/tenants/{id}/…``, second factor on the money
decisions (В-8), convert-to-salon (В-2), trial from the first
publication (В-10), the lifecycle events the bot mirrors, and
``archived`` on the availability door.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from appointments.models import OutboxEvent
from appointments.tasks import EVENT_HANDLERS
from billing import tasks
from billing.charges import (
    charge_subscription,
    settle_charge_success,
    start_pending_trials,
)
from billing.models import (
    BillingConsent,
    BillingInvoice,
    BillingPayment,
    SpecialistSubscription,
    TariffPlan,
)
from billing.services import can_accept_booking
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

RUNTIME = "test-runtime-token-2984"  # noqa: S105
OWNER = "bot:max:2984001"
STRANGER = "bot:max:2984999"

NEW_TOPICS = {
    "subscription.trial_started": {"tenant_id", "tariff", "trial_ends_at"},
    "subscription.renewed": {
        "tenant_id", "tariff", "period_end", "amount", "masters_count",
    },
    "subscription.frozen": {"tenant_id", "reason"},
    "subscription.canceled": {"tenant_id", "effective_at"},
    "subscription.plan_changed": {"tenant_id", "tariff", "effective_from"},
}

_seq = iter(range(1000, 9999))


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME


def _client(actor: str | None = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _admin_of(tenant: Tenant, external_id: str) -> User:
    n = next(_seq)
    real = User.objects.create_user(
        username=f"c3_admin_{n}", password="x", role="client",  # noqa: S106
        phone=f"+79993{n:06d}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False,
        linked_user=real,
    )
    TenantUserRelationship.objects.create(
        user=real, tenant=tenant, role=TenantUserRelationship.Role.ADMIN,
        is_active=True,
    )
    return real


def _master(tenant, *, published=True):
    n = next(_seq)
    user = User.objects.create_user(
        username=f"c3_master_{n}", password="x", role="specialist",  # noqa: S106
        phone=f"+79994{n:06d}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.status = "active" if published else "draft"
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


def _url(tenant, door: str) -> str:
    return f"/api/v1/internal/billing/tenants/{tenant.pk}/{door}/"


def _events(topic: str) -> list[dict]:
    return [
        row.payload["data"]
        for row in OutboxEvent.objects.filter(topic=topic).order_by("created_at")
    ]


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name="C3 Salon", slug="c3-salon")


@pytest.fixture
def solo(db):
    return Tenant.objects.create(
        name="C3 Solo", slug="c3-solo", kind=Tenant.Kind.SOLO,
    )


@pytest.fixture
def owner(salon):
    return _admin_of(salon, OWNER)


@pytest.fixture
def billing_on(settings):
    settings.BILLING_ENABLED = True
    settings.BILLING_CHARGES_ENABLED = False


class TestSwitchedOff:
    @pytest.mark.parametrize("method,door,body", [
        ("post", "subscription", {"idempotency_key": "k"}),
        ("get", "status", None),
        ("post", "card-setup", {"return_url": "https://t.me/a"}),
        ("post", "pay-debt", {}),
        ("post", "cancel", {"idempotency_key": "k"}),
        ("post", "convert-to-salon", {"idempotency_key": "k", "confirmed_price": "990.00"}),
    ])
    def test_every_door_404_and_writes_nothing(
        self, settings, salon, owner, method, door, body,
    ):
        settings.BILLING_ENABLED = False
        resp = getattr(_client(), method)(_url(salon, door), body, format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "BILLING_DISABLED"
        assert not SpecialistSubscription.objects.exists()
        salon.refresh_from_db()
        assert salon.kind == Tenant.Kind.SALON


@pytest.mark.usefixtures("billing_on")
class TestOpenAccount:
    def test_201_then_200_same(self, salon, owner):
        _master(salon)
        first = _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        again = _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        assert first.status_code == 201
        assert again.status_code == 200
        assert first.json()["data"] == again.json()["data"]
        data = first.json()["data"]
        assert data["status"] == "trial"
        assert data["tariff"] == "salon"
        assert data["price_preview"] == "990.00"
        sub = SpecialistSubscription.objects.get(tenant=salon)
        assert sub.trial_ends_at is not None
        assert data["trial_ends_at"] == sub.trial_ends_at.isoformat()
        assert sub.user_id == owner.pk
        assert len(_events("subscription.trial_started")) == 1

    def test_tariff_follows_kind_not_body(self, solo):
        _admin_of(solo, "bot:max:2984002")
        resp = _client(None).post(
            _url(solo, "subscription"),
            {"idempotency_key": "a", "tariff": "salon"}, format="json",
        )
        assert resp.json()["data"]["tariff"] == "solo"
        assert resp.json()["data"]["price_preview"] == "690.00"

    def test_trial_waits_for_first_publication(self, salon, owner):
        _master(salon, published=False)
        resp = _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        assert resp.json()["data"]["trial_ends_at"] is None
        assert start_pending_trials() == 0
        SpecialistProfile.objects.filter(tenant=salon).update(status="active")
        assert start_pending_trials(today=date(2026, 10, 21)) == 1
        sub = SpecialistSubscription.objects.get(tenant=salon)
        assert sub.trial_ends_at == date(2026, 11, 21)
        assert start_pending_trials() == 0  # idempotent

    def test_no_admin_409(self, salon):
        resp = _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "NO_TENANT_ADMIN"

    def test_idempotency_key_required(self, salon, owner):
        resp = _client(None).post(_url(salon, "subscription"), {}, format="json")
        assert resp.status_code == 400

    def test_status_shape(self, salon, owner):
        for _ in range(4):
            _master(salon)
        _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        data = _client(None).get(_url(salon, "status")).json()["data"]
        sub = data["subscription"]
        assert data["tenant_id"] == str(salon.pk)
        assert sub["masters_count"] == 4
        assert sub["extra_masters_amount"] == "150.00"
        assert sub["price_preview"] == "1140.00"
        assert sub["frozen_at"] is None
        assert set(sub) >= {
            "status", "tariff", "trial_ends_at", "current_period_end",
            "frozen_reason", "card", "pending_tariff",
        }

    def test_status_without_account(self, salon):
        data = _client(None).get(_url(salon, "status")).json()["data"]
        assert data["subscription"] is None


@pytest.mark.usefixtures("billing_on")
class TestSecondFactor:
    @pytest.mark.parametrize("door,body", [
        ("card-setup", {"return_url": "https://t.me/a"}),
        ("cancel", {"idempotency_key": "k"}),
        ("convert-to-salon", {"idempotency_key": "k", "confirmed_price": "990.00"}),
    ])
    def test_unnamed_403_stranger_404(self, salon, owner, door, body):
        _admin_of(Tenant.objects.create(name="Other", slug="c3-other"), STRANGER)
        unnamed = _client(None).post(_url(salon, door), body, format="json")
        stranger = _client(STRANGER).post(_url(salon, door), body, format="json")
        assert unnamed.status_code == 403
        assert stranger.status_code == 404

    def test_card_setup_503_while_charges_off(self, salon, owner):
        resp = _client().post(_url(salon, "card-setup"), {"return_url": "https://t.me/a"}, format="json")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "BILLING_CHARGES_DISABLED"

    def test_card_setup_consent_mismatch_409(self, salon, owner):
        resp = _client().post(
            _url(salon, "card-setup"),
            {"return_url": "https://t.me/a", "consent_version": "offer-old"},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONSENT_VERSION_MISMATCH"


@pytest.mark.usefixtures("billing_on")
class TestCancel:
    @pytest.fixture
    def account(self, salon, owner):
        sub = SpecialistSubscription.objects.create(
            user=owner, tenant=salon,
            tariff=TariffPlan.objects.get(code="salon"),
            status=SpecialistSubscription.Status.ACTIVE,
            current_period_start=date(2026, 10, 1),
            current_period_end=date(2026, 10, 31),
        )
        BillingConsent.objects.create(user=owner, document_version="v")
        return sub

    def test_cancel_to_end_of_paid_period(self, salon, account):
        first = _client().post(_url(salon, "cancel"), {"idempotency_key": "c"}, format="json")
        again = _client().post(_url(salon, "cancel"), {"idempotency_key": "c"}, format="json")
        assert first.status_code == again.status_code == 200
        assert first.json()["data"] == {"status": "canceled", "effective_at": "2026-11-01"}
        assert again.json()["data"] == first.json()["data"]
        account.refresh_from_db()
        assert account.status == SpecialistSubscription.Status.CANCELED
        assert not BillingConsent.objects.filter(revoked_at__isnull=True).exists()
        assert _events("subscription.canceled") == [
            {"tenant_id": str(salon.pk), "effective_at": "2026-11-01"},
        ]

    def test_bookings_open_until_paid_end(self, salon, account):
        master = _master(salon)
        _client().post(_url(salon, "cancel"), {"idempotency_key": "c"}, format="json")
        with patch("billing.services.timezone.localdate", return_value=date(2026, 10, 31)):
            assert can_accept_booking(master.user_id, salon.pk).ok
        with patch("billing.services.timezone.localdate", return_value=date(2026, 11, 1)):
            assert not can_accept_booking(master.user_id, salon.pk).ok


@pytest.mark.usefixtures("billing_on")
class TestConvertToSalon:
    @pytest.fixture
    def solo_account(self, solo):
        admin = _admin_of(solo, OWNER)
        return SpecialistSubscription.objects.create(
            user=admin, tenant=solo, tariff=TariffPlan.objects.get(code="solo"),
            status=SpecialistSubscription.Status.ACTIVE,
            current_period_start=date(2026, 10, 1),
            current_period_end=date(2026, 10, 31),
            payment_method_id="pm_c3",
        )

    def _convert(self, solo, price="990.00"):
        return _client().post(
            _url(solo, "convert-to-salon"),
            {"idempotency_key": "x", "confirmed_price": price}, format="json",
        )

    def test_kind_now_price_next_period(self, solo, solo_account):
        resp = self._convert(solo)
        assert resp.status_code == 200
        assert resp.json()["data"] == {"tariff": "salon", "effective_from": "2026-11-01"}
        solo.refresh_from_db()
        solo_account.refresh_from_db()
        assert solo.kind == Tenant.Kind.SALON
        assert solo_account.tariff.code == "solo"
        assert solo_account.pending_tariff.code == "salon"
        assert _events("subscription.plan_changed") == [{
            "tenant_id": str(solo.pk), "tariff": "salon",
            "effective_from": "2026-11-01",
        }]
        # repeat → same answer, no second event
        assert self._convert(solo).json()["data"]["effective_from"] == "2026-11-01"
        assert len(_events("subscription.plan_changed")) == 1

    def test_next_charge_is_salon_and_renewed_carries_it(self, settings, solo, solo_account):
        settings.BILLING_CHARGES_ENABLED = True
        self._convert(solo)
        with patch("billing.charges.BillingYooKassaClient") as client_cls:
            client_cls.return_value.create_recurrent_payment.return_value = {
                "provider_payment_id": "yk_c3_conv", "status": "succeeded",
            }
            invoice = charge_subscription(
                subscription=solo_account, today=date(2026, 11, 1),
            )
        solo_account.refresh_from_db()
        assert solo_account.tariff.code == "salon"
        assert solo_account.pending_tariff is None
        assert invoice.total_amount == Decimal("990.00")
        renewed = _events("subscription.renewed")
        assert renewed[-1]["tariff"] == "salon"
        assert set(renewed[-1]) == NEW_TOPICS["subscription.renewed"]

    def test_price_mismatch_409_nothing_changes(self, solo, solo_account):
        resp = self._convert(solo, price="690.00")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PRICE_MISMATCH"
        solo.refresh_from_db()
        assert solo.kind == Tenant.Kind.SOLO

    def test_already_salon_409(self, salon, owner):
        resp = _client().post(
            _url(salon, "convert-to-salon"),
            {"idempotency_key": "x", "confirmed_price": "990.00"}, format="json",
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ALREADY_SALON"

    def test_second_master_can_then_be_provisioned(self, solo, solo_account):
        from tenants.salon_specialist_provisioning import (
            SalonSpecialistRefused,
            provision_salon_specialist,
        )

        with pytest.raises(SalonSpecialistRefused):
            provision_salon_specialist(
                tenant_id=solo.pk, external_user_id="bot:max:2984777",
                display_name="Second",
            )
        self._convert(solo)
        provision_salon_specialist(
            tenant_id=solo.pk, external_user_id="bot:max:2984777",
            display_name="Second",
        )


@pytest.mark.usefixtures("billing_on")
class TestLifecycleEvents:
    def test_every_new_topic_has_a_local_handler(self):
        for topic in NEW_TOPICS:
            assert OutboxEvent.Topic(topic) in EVENT_HANDLERS

    def test_trial_end_without_card_emits_frozen(self, settings, salon, owner):
        settings.BILLING_CHARGES_ENABLED = True
        _master(salon)
        with patch("billing.charges.timezone.localdate", return_value=date(2026, 9, 1)):
            _client(None).post(_url(salon, "subscription"), {"idempotency_key": "a"}, format="json")
        with patch("billing.tasks.timezone.localdate", return_value=date(2026, 10, 1)):
            tasks.charge_subscriptions_monthly()
        assert _events("subscription.frozen") == [
            {"tenant_id": str(salon.pk), "reason": "trial_ended_no_card"},
        ]
        started = _events("subscription.trial_started")
        assert set(started[0]) == NEW_TOPICS["subscription.trial_started"]

    def test_settle_emits_renewed(self, salon, owner):
        sub = SpecialistSubscription.objects.create(
            user=owner, tenant=salon, tariff=TariffPlan.objects.get(code="salon"),
            status=SpecialistSubscription.Status.ACTIVE,
            current_period_end=date(2026, 10, 31),
        )
        invoice = BillingInvoice.objects.create(
            subscription=sub, period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 30), total_amount=Decimal("1140.00"),
            masters_count=4, idempotency_key="charge:c3-renew",
        )
        payment = BillingPayment.objects.create(
            invoice=invoice, kind=BillingPayment.Kind.RECURRENT,
            amount=Decimal("1140.00"), idempotency_key="pay:c3-renew",
            provider_payment_id="yk_c3_renew",
        )
        settle_charge_success(subscription=sub, invoice=invoice, payment_row=payment)
        assert _events("subscription.renewed") == [{
            "tenant_id": str(salon.pk), "tariff": "salon",
            "period_end": "2026-11-30", "amount": "1140.00", "masters_count": 4,
        }]

    @pytest.mark.parametrize("enabled,started", [(False, 0), (True, 1)])
    def test_trial_sweep_needs_billing_only(self, settings, salon, owner, enabled, started):
        SpecialistSubscription.objects.create(
            user=owner, tenant=salon, tariff=TariffPlan.objects.get(code="salon"),
        )
        _master(salon)
        settings.BILLING_ENABLED = enabled
        assert tasks.start_billing_trials() == started


def test_new_topics_have_versions():
    from appointments.infrastructure.outbox.envelope import EVENT_VERSIONS

    for topic in NEW_TOPICS:
        assert EVENT_VERSIONS[topic] == 1
