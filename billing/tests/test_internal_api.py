"""C2 endpoint tests — GET /api/v1/internal/billing/specialists/{id}/status/.

Runs under the W2 shim (--ds=billing.tests.settings_w2): billing app +
urlconf are not in the canonical settings yet (B-1/B-5 W1 patches).
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
            "date": "2026-07-01",
        }
        assert data["fees"] == {"pending_total": "90.00", "pending_count": 1}
        assert data["last_invoice"] is None
