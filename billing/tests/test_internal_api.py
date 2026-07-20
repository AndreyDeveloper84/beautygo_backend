"""C2 endpoint tests — GET /api/v1/internal/billing/specialists/{id}/status/.

Runs under the canonical settings (billing app + urlconf are wired by
W1's P1/P3 patches).
"""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from billing.models import BookingFee


URL_TMPL = "/api/v1/internal/billing/specialists/{user_id}/status/"


@pytest.fixture
def bearer_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = "test-bearer"
    return "test-bearer"


@pytest.fixture
def api(bearer_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer_token}")
    return client


class TestAuth:
    def test_requires_bearer(self, db, specialist):
        resp = APIClient().get(URL_TMPL.format(user_id=specialist.user_id))
        assert resp.status_code in (401, 403)


class TestBillingStatusEndpoint:
    def test_unknown_specialist_404(self, api, db):
        resp = api.get(URL_TMPL.format(user_id=uuid4()))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SPECIALIST_NOT_FOUND"

    def test_none_shape_200(self, api, specialist):
        resp = api.get(URL_TMPL.format(user_id=specialist.user_id))
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "specialist_id": str(specialist.user_id),
            "subscription": {
                "status": "none",
                "tariff": None,
                "current_period_end": None,
                "next_charge": None,
            },
            "fees": {"pending_total": "0.00", "pending_count": 0},
            "last_invoice": None,
        }

    def test_active_shape_200(self, api, specialist, subscription, appointment):
        BookingFee.objects.create(
            appointment=appointment, subscription=subscription,
            amount=Decimal("90.00"), period_start=date(2026, 7, 1),
        )
        resp = api.get(URL_TMPL.format(user_id=specialist.user_id))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["subscription"]["status"] == "active"
        assert data["subscription"]["tariff"] == "solo"
        assert data["subscription"]["next_charge"] == {
            "subscription_amount": "690.00",
            "fees_amount": "90.00",
            "total_amount": "780.00",
            "date": "2026-08-01",
        }
        assert data["fees"] == {"pending_total": "90.00", "pending_count": 1}
        assert data["last_invoice"] is None


class TestCardSetupEndpoint:
    URL = "/api/v1/internal/billing/specialists/{user_id}/card-setup/"

    def test_requires_bearer(self, db, specialist):
        resp = APIClient().post(self.URL.format(user_id=specialist.user_id), {})
        assert resp.status_code in (401, 403)

    def test_unknown_specialist_404(self, api, db):
        resp = api.post(
            self.URL.format(user_id=uuid4()),
            {"tariff": "solo", "return_url": "https://x.example"},
            format="json",
        )
        assert resp.status_code == 404

    def test_validation_error_on_bad_body(self, api, specialist):
        resp = api.post(self.URL.format(user_id=specialist.user_id), {}, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_happy_path_returns_confirmation_url(self, api, specialist):
        from unittest.mock import patch

        from billing.charges import CardSetupResult

        fake = CardSetupResult(
            subscription_id=uuid4(), invoice_id=uuid4(),
            confirmation_url="https://pay.example/confirm",
        )
        with patch("billing.internal_api.start_card_setup", return_value=fake) as setup:
            resp = api.post(
                self.URL.format(user_id=specialist.user_id),
                {"tariff": "solo", "return_url": "https://miniapp.example/back"},
                format="json",
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["confirmation_url"] == "https://pay.example/confirm"
        assert data["subscription_id"] == str(fake.subscription_id)
        _, kwargs = setup.call_args
        assert kwargs["tariff_code"] == "solo"
        assert kwargs["tenant"] is None

    def test_salon_without_tenant_400(self, api, specialist):
        from users.models import SpecialistProfile as SP

        SP.objects.filter(pk=specialist.pk).update(tenant=None)
        resp = api.post(
            self.URL.format(user_id=specialist.user_id),
            {"tariff": "salon", "return_url": "https://x.example"},
            format="json",
        )
        assert resp.status_code == 400

    def test_provider_config_error_503(self, api, specialist):
        from unittest.mock import patch

        from billing.yookassa import BillingPaymentConfigError

        with patch(
            "billing.internal_api.start_card_setup",
            side_effect=BillingPaymentConfigError("no creds"),
        ):
            resp = api.post(
                self.URL.format(user_id=specialist.user_id),
                {"tariff": "solo", "return_url": "https://x.example"},
                format="json",
            )
        assert resp.status_code == 503

    def test_provider_client_error_502(self, api, specialist):
        from unittest.mock import patch

        from billing.yookassa import BillingPaymentClientError

        with patch(
            "billing.internal_api.start_card_setup",
            side_effect=BillingPaymentClientError("network"),
        ):
            resp = api.post(
                self.URL.format(user_id=specialist.user_id),
                {"tariff": "solo", "return_url": "https://x.example"},
                format="json",
            )
        assert resp.status_code == 502
