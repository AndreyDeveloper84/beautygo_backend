"""Integration tests for /api/v1/nutrition/internal/{food-log,summary}/ (DRF-247).

Service-to-service mirror endpoints used by the MAX bot. Auth via
X-Service-Token + X-External-User-ID. Idempotency via X-Idempotency-Key
(forwarded to FoodLogService).
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, FoodScan
from users.models import User


pytestmark = pytest.mark.django_db


LOG_URL = "/api/v1/nutrition/internal/food-log/"
SUMMARY_URL = "/api/v1/nutrition/internal/summary/"
SERVICE_TOKEN = "test-token-DRF-247"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:777", role="client", is_proxy=True,
    )


@pytest.fixture
def scan_with_nutrition(proxy_user):
    return FoodScan.objects.create(
        user=proxy_user,
        dish_name="Борщ",
        confidence=0.92,
        portion_g=300,
        ingredients=["свёкла", "капуста"],
        provider_used="openai",
        nutrition={
            "kcal": 250.0,
            "protein_g": 8.0,
            "fat_g": 6.0,
            "carbs_g": 35.0,
            "source": "seed",
            "matched_dish": "борщ",
        },
    )


# ---------------------------------------------------------------------------
# /internal/food-log/
# ---------------------------------------------------------------------------


class TestInternalFoodLog:
    def test_missing_service_token_returns_401(self):
        """DRF returns 401 NotAuthenticated when no auth was provided."""
        c = APIClient()
        resp = c.post(
            LOG_URL,
            {"meal_type": "breakfast", "portion_multiplier": 1.0},
            format="json",
            HTTP_X_EXTERNAL_USER_ID="bot:777",
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_logs_from_scan_with_idempotency(self, scan_with_nutrition):
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:777",
            "HTTP_X_IDEMPOTENCY_KEY": "idem-1",
        }
        body = {
            "scan_id": str(scan_with_nutrition.id),
            "meal_type": "lunch",
            "portion_multiplier": 1.0,
        }
        r1 = c.post(LOG_URL, body, format="json", **headers)
        assert r1.status_code == status.HTTP_201_CREATED, r1.json()
        # Second call with same idempotency key — must not duplicate.
        r2 = c.post(LOG_URL, body, format="json", **headers)
        assert r2.status_code == status.HTTP_201_CREATED
        assert FoodLog.objects.count() == 1
        assert r1.json()["data"]["id"] == r2.json()["data"]["id"]

    def test_invalid_external_id_returns_400(self):
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "no-colon-here",
        }
        resp = c.post(
            LOG_URL,
            {"meal_type": "lunch", "portion_multiplier": 1.0},
            format="json",
            **headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# /internal/summary/
# ---------------------------------------------------------------------------


class TestInternalSummary:
    def test_returns_zero_envelope_for_empty_day(self, proxy_user):
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:777",
        }
        resp = c.get(SUMMARY_URL, **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["calories_total"] == 0
        assert body["entries"] == []

    def test_aggregates_food_log_for_day(self, proxy_user):
        # Insert two log rows on today's UTC day.
        FoodLog.objects.create(
            user=proxy_user,
            dish_name="Борщ",
            portion_multiplier=1.0,
            calories=250, protein_g=8, fat_g=6, carbs_g=35,
            meal_type="lunch",
            logged_at=datetime.now(dt_tz.utc),
        )
        FoodLog.objects.create(
            user=proxy_user,
            dish_name="Каша",
            portion_multiplier=1.0,
            calories=150, protein_g=5, fat_g=2, carbs_g=30,
            meal_type="breakfast",
            logged_at=datetime.now(dt_tz.utc),
        )

        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:777",
        }
        resp = c.get(SUMMARY_URL, **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["calories_total"] == 400
        assert body["protein_g"] == 13
        assert len(body["entries"]) == 2
