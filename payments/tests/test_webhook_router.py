"""Webhook router tests (amendment A–L) — one YooKassa URL, two flows.

`POST /api/v1/payments/webhook/` routes by payment ownership:
payments.Payment (client) → billing.BillingPayment (W2) → exactly one
legacy card-binding probe → sanitized unknown marker + 200. Covers:
dispatch, collision, duplicates, event-aware id extraction, security
order, IP trust model, Basic hygiene, settings CIDR validation.
"""
from __future__ import annotations

import base64
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from billing.models import (
    BillingInvoice,
    BillingPayment,
    SpecialistSubscription,
    TariffPlan,
)
from payments.models import Payment
from payments.views import extract_provider_payment_id
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

WEBHOOK_URL = "/api/v1/payments/webhook/"
PROVIDER_ID = "yk_router_1"


@pytest.fixture(autouse=True)
def _allow_all(settings):
    settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = []


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="router_spec", password="x", role="specialist",
        phone="+79995000001",
    )


@pytest.fixture
def subscription(db, specialist_user):
    return SpecialistSubscription.objects.create(
        user=specialist_user,
        tariff=TariffPlan.objects.get(code="solo"),
        status=SpecialistSubscription.Status.ACTIVE,
    )


@pytest.fixture
def invoice(db, subscription):
    return BillingInvoice.objects.create(
        subscription=subscription,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        subscription_amount=Decimal("690.00"),
        fees_amount=Decimal("0.00"),
        total_amount=Decimal("690.00"),
        idempotency_key=f"inv-{uuid.uuid4().hex[:8]}",
    )


@pytest.fixture
def billing_payment(db, invoice):
    return BillingPayment.objects.create(
        invoice=invoice,
        kind=BillingPayment.Kind.RECURRENT,
        amount=Decimal("690.00"),
        idempotency_key=f"bp-{uuid.uuid4().hex[:8]}",
        provider_payment_id=PROVIDER_ID,
    )


@pytest.fixture
def client_payment(db, specialist_user):
    cat = ServiceCategory.objects.create(name="R Cat", slug="r-cat")
    profile = SpecialistProfile.objects.get(user=specialist_user)
    service = Service.objects.create(
        specialist=profile, category=cat, name="R Service",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )
    client = User.objects.create_user(
        username="router_client", password="x", role="client",
        phone="+79995000002",
    )
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    appt = Appointment.objects.create(
        client=client, specialist=profile, service=service,
        start_datetime=now + timedelta(hours=3),
        end_datetime=now + timedelta(hours=4),
        status=Appointment.Status.CONFIRMED, price=service.price,
    )
    return Payment.objects.create(
        appointment=appt, amount=service.price,
        status=Payment.Status.AUTHORIZED,
        specialist_income=Decimal("1910.00"), platform_fee=Decimal("90.00"),
        provider="yookassa", provider_payment_id=PROVIDER_ID,
        capture_state=Payment.CaptureState.SCHEDULED,
    )


def _api():
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    return c


def _post(event, obj, **kw):
    return _api().post(WEBHOOK_URL, {"event": event, "object": obj},
                       format="json", **kw)


def _info(status="succeeded", **kw):
    info = {
        "provider_payment_id": PROVIDER_ID,
        "status": status,
        "paid": status == "succeeded",
        "expires_at": None,
        "refunded_amount": Decimal("0"),
        "metadata": {},
        "payment_method": {"id": "", "saved": False, "last4": "", "brand": ""},
    }
    info.update(kw)
    return info


# ---------------------------------------------------------------------------
# Dispatch (router core)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestBillingDispatch:
    def test_billing_payment_dispatches_to_billing_handler(
        self, billing_payment,
    ):
        with patch(
            "billing.charges.handle_webhook_event", return_value="ok",
        ) as handler:
            r = _post("payment.succeeded", {"id": PROVIDER_ID})
        assert r.status_code == 200
        handler.assert_called_once_with(
            event="payment.succeeded", provider_payment_id=PROVIDER_ID,
        )

    def test_billing_full_path_updates_payment_and_invoice(
        self, billing_payment, invoice,
    ):
        """Invariant A: BillingInvoice never owns provider_payment_id —
        the webhook finds the BillingPayment row and the W2 handler
        settles BOTH the payment and its linked invoice."""
        client = MagicMock()
        client.get_payment_info.return_value = {
            "status": "succeeded",
            "payment_method_id": "",
            "payment_method_saved": False,
        }
        with patch("billing.charges.BillingYooKassaClient", return_value=client):
            r = _post("payment.succeeded", {"id": PROVIDER_ID})
        assert r.status_code == 200
        billing_payment.refresh_from_db()
        invoice.refresh_from_db()
        assert billing_payment.status == BillingPayment.Status.SUCCEEDED
        assert invoice.status == BillingInvoice.Status.PAID


@pytest.mark.django_db
def test_unknown_local_payment_performs_legacy_binding_probe_then_acks():
    """B: an id unknown to both tables triggers EXACTLY ONE provider
    re-fetch (the legacy card-binding probe); no purpose in metadata →
    billing handler NOT called → ack 200."""
    svc = MagicMock()
    svc.get_payment_info.return_value = _info(metadata={"purpose": "other"})
    with patch("payments.views._get_yookassa", return_value=svc), patch(
        "billing.charges.handle_webhook_event",
    ) as handler:
        r = _post("payment.succeeded", {"id": "yk_unknown_1"})
    assert r.status_code == 200
    assert svc.get_payment_info.call_count == 1
    handler.assert_not_called()


@pytest.mark.django_db
class TestCollisionAndDuplicates:
    def test_collision_client_handler_wins_and_alerts(
        self, client_payment, billing_payment,
    ):
        """D: same provider id in BOTH tables → only the client handler
        runs + collision alert is logged (never silent)."""
        svc = MagicMock()
        svc.get_payment_info.return_value = _info(status="succeeded")
        with patch("payments.views._get_yookassa", return_value=svc), patch(
            "billing.charges.handle_webhook_event",
        ) as billing_handler, patch("payments.views.logger") as mock_logger:
            r = _post("payment.succeeded", {"id": PROVIDER_ID})
        assert r.status_code == 200
        billing_handler.assert_not_called()
        client_payment.refresh_from_db()
        assert client_payment.status == Payment.Status.PAID
        assert any(
            "webhook.collision" in str(call)
            for call in mock_logger.error.call_args_list
        )

    def test_duplicate_delivery_second_is_duplicate(
        self, client_payment,
    ):
        """D: the same payment.succeeded delivered twice → first ok,
        second duplicate; state + outbox side effects happen once."""
        svc = MagicMock()
        svc.get_payment_info.return_value = _info(status="succeeded")
        from appointments.models import OutboxEvent
        with patch("payments.views._get_yookassa", return_value=svc):
            r1 = _post("payment.succeeded", {"id": PROVIDER_ID})
            r2 = _post("payment.succeeded", {"id": PROVIDER_ID})
        assert r1.json()["status"] == "ok"
        assert r2.json()["status"] == "duplicate"
        client_payment.refresh_from_db()
        assert client_payment.status == Payment.Status.PAID
        assert OutboxEvent.objects.filter(
            topic=OutboxEvent.Topic.PAYMENT_CAPTURED,
        ).count() == 1


# ---------------------------------------------------------------------------
# Event-aware extraction (E)
# ---------------------------------------------------------------------------

class TestExtractProviderPaymentId:
    def test_payment_event_uses_object_id(self):
        assert extract_provider_payment_id(
            "payment.succeeded", {"id": "p1"},
        ) == "p1"

    def test_refund_uses_payment_id_not_refund_id(self):
        assert extract_provider_payment_id(
            "refund.succeeded", {"id": "refund_1", "payment_id": "p1"},
        ) == "p1"

    def test_unsupported_event_returns_none(self):
        assert extract_provider_payment_id("payout.succeeded", {"id": "x"}) is None


@pytest.mark.django_db
def test_refund_routed_by_payment_id(client_payment):
    """E: a refund webhook names the REFUND id in object.id — routing
    must follow object.payment_id to the payment."""
    client_payment.status = Payment.Status.PAID
    client_payment.save(update_fields=["status"])
    svc = MagicMock()
    svc.get_payment_info.return_value = _info(
        status="succeeded", refunded_amount=Decimal("2000.00"),
    )
    with patch("payments.views._get_yookassa", return_value=svc):
        r = _post("refund.succeeded", {
            "id": "refund_xyz", "payment_id": PROVIDER_ID,
        })
    assert r.status_code == 200
    client_payment.refresh_from_db()
    assert client_payment.status == Payment.Status.REFUNDED


# ---------------------------------------------------------------------------
# Security order (F) + IP trust model (C) + Basic hygiene (I)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSecurityOrder:
    def test_no_db_no_handler_no_api_on_403(self, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        with patch("payments.views.Payment.objects") as pm, patch(
            "billing.charges.handle_webhook_event",
        ) as bh, patch("payments.views._get_yookassa") as yk:
            r = _post("payment.succeeded", {"id": PROVIDER_ID},
                      REMOTE_ADDR="1.2.3.4")
        assert r.status_code == 403
        pm.assert_not_called()
        bh.assert_not_called()
        yk.assert_not_called()

    def test_no_db_no_handler_no_api_on_401(self, settings):
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = "yookassa"
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = "secret"
        with patch("payments.views.Payment.objects") as pm, patch(
            "billing.charges.handle_webhook_event",
        ) as bh, patch("payments.views._get_yookassa") as yk:
            r = _post("payment.succeeded", {"id": PROVIDER_ID})
        assert r.status_code == 401
        pm.assert_not_called()
        bh.assert_not_called()
        yk.assert_not_called()


@pytest.mark.django_db
class TestIPTrustModel:
    @pytest.mark.parametrize("ip", ["185.71.76.5", "2a02:5180::1"])
    def test_direct_allowed_ipv4_and_ipv6(self, settings, ip):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = [
            "185.71.76.0/27", "2a02:5180::/32",
        ]
        r = _post("payment.succeeded", {"id": "yk_unknown_ip"},
                  REMOTE_ADDR=ip)
        assert r.status_code == 200

    def test_forbidden_direct_ip(self, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        r = _post("payment.succeeded", {"id": "x"}, REMOTE_ADDR="1.2.3.4")
        assert r.status_code == 403

    def test_via_trusted_proxy(self, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        settings.YOOKASSA_WEBHOOK_TRUSTED_PROXY_IPS = ["10.0.0.1"]
        r = _post("payment.succeeded", {"id": "yk_unknown_proxy"},
                  REMOTE_ADDR="10.0.0.1",
                  HTTP_X_FORWARDED_FOR="185.71.76.10")
        assert r.status_code == 200

    def test_spoofed_xff_from_untrusted_peer(self, settings):
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = ["185.71.76.0/27"]
        settings.YOOKASSA_WEBHOOK_TRUSTED_PROXY_IPS = ["127.0.0.1"]
        r = _post("payment.succeeded", {"id": "x"},
                  REMOTE_ADDR="1.2.3.4",
                  HTTP_X_FORWARDED_FOR="185.71.76.10")
        assert r.status_code == 403

    @pytest.mark.parametrize("ip", ["77.75.156.11", "77.75.156.35"])
    def test_exact_single_ip_entries(self, settings, ip):
        """The official list carries these two as single-IP entries —
        they must match verbatim, not as a CIDR."""
        settings.YOOKASSA_WEBHOOK_ALLOWED_IPS = [
            "77.75.156.11", "77.75.156.35",
        ]
        r = _post("payment.succeeded", {"id": "yk_exact"}, REMOTE_ADDR=ip)
        assert r.status_code == 200


@pytest.mark.django_db
class TestBasicHygiene:
    @pytest.fixture(autouse=True)
    def _creds(self, settings):
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_USER = "yookassa"
        settings.YOOKASSA_WEBHOOK_BASIC_AUTH_PASS = "secret-pass-1"

    def test_invalid_base64_401(self):
        r = _post("payment.succeeded", {"id": "x"},
                  HTTP_AUTHORIZATION="Basic !!!garbage!!!")
        assert r.status_code == 401

    def test_missing_header_401(self):
        r = _post("payment.succeeded", {"id": "x"})
        assert r.status_code == 401

    def test_bearer_scheme_401(self):
        r = _post("payment.succeeded", {"id": "x"},
                  HTTP_AUTHORIZATION="Bearer some-token")
        assert r.status_code == 401

    def test_realm_header_present(self):
        r = _post("payment.succeeded", {"id": "x"})
        assert r.headers["WWW-Authenticate"] == 'Basic realm="YooKassa webhook"'

    def test_no_password_in_logs(self):
        with patch("payments.views.logger") as mock_logger:
            token = base64.b64encode(b"yookassa:wrong-pass").decode()
            _post("payment.succeeded", {"id": "x"},
                  HTTP_AUTHORIZATION=f"Basic {token}")
        for call in mock_logger.method_calls:
            rendered = str(call)
            assert "secret-pass-1" not in rendered
            assert "Authorization" not in rendered


@pytest.mark.django_db
class TestUnknownAuditMarker:
    def test_unknown_webhook_sanitized_audit_marker(self):
        """H: unknown payment → warning with event + provider id only —
        no payload, no card data."""
        svc = MagicMock()
        svc.get_payment_info.return_value = _info(metadata={"purpose": "?"})
        with patch("payments.views._get_yookassa", return_value=svc), patch(
            "payments.views.logger",
        ) as mock_logger:
            r = _post("payment.canceled", {"id": "yk_unknown_9",
                                           "card": {"last4": "4242"}})
        assert r.status_code == 200
        rendered = [str(call) for call in mock_logger.method_calls]
        assert any(
            "webhook.unknown_payment" in m and "yk_unknown_9" in m
            for m in rendered
        )
        assert not any("4242" in m for m in rendered)


# ---------------------------------------------------------------------------
# Settings CIDR validation (J)
# ---------------------------------------------------------------------------

class TestSettingsCidrValidation:
    def test_valid_networks_pass(self):
        from djangoProject.settings.base import _validated_networks
        assert _validated_networks(
            ["185.71.76.0/27", " 77.75.156.11 ", "2a02:5180::/32"],
            setting_name="T",
        ) == ["185.71.76.0/27", "77.75.156.11", "2a02:5180::/32"]

    def test_invalid_network_is_config_error(self):
        from django.core.exceptions import ImproperlyConfigured
        from djangoProject.settings.base import _validated_networks
        with pytest.raises(ImproperlyConfigured):
            _validated_networks(["not-a-network"], setting_name="T")
