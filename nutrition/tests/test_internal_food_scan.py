"""Integration tests for POST /api/v1/nutrition/internal/scan/ (DRF-246).

Service-to-service endpoint used by the MAX bot. Authenticates via
X-Service-Token (constant_time_compare) + X-External-User-ID (resolves
to ProxyUser via users.services.resolve_external_user).
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import ScanResult
from nutrition.services.food_scanner_router import RouterResult
from users.models import User


pytestmark = pytest.mark.django_db


INTERNAL_URL = "/api/v1/nutrition/internal/scan/"
SERVICE_TOKEN = "test-service-token-DRF-246"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


def _image_bytes() -> bytes:
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _upload(name: str = "meal.jpg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _image_bytes(), content_type="image/jpeg")


def _scan_result(dish: str = "Борщ", confidence: float = 0.9) -> ScanResult:
    return ScanResult(
        dish_name=dish,
        confidence=confidence,
        portion_g=300,
        ingredients=["свёкла"],
        provider="openai",
        latency_ms=400,
        raw_response={"ok": True},
    )


def _post(client: APIClient, *, token: str, external_id: str, **extra):
    headers = {
        "HTTP_X_SERVICE_TOKEN": token,
        "HTTP_X_EXTERNAL_USER_ID": external_id,
    }
    return client.post(
        INTERNAL_URL,
        {"image": _upload(), **extra},
        format="multipart",
        **headers,
    )


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


class TestServiceAccountAuth:
    def test_missing_token_returns_401(self):
        """DRF returns 401 NotAuthenticated when permission fails for an
        unauthenticated request — there's no JWT to evaluate against
        IsServiceAccount, so the request is treated as unauthenticated."""
        c = APIClient()
        resp = c.post(
            INTERNAL_URL,
            {"image": _upload()},
            format="multipart",
            HTTP_X_EXTERNAL_USER_ID="bot:12345",
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_wrong_token_returns_401(self):
        c = APIClient()
        resp = _post(c, token="not-the-token", external_id="bot:12345")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_jwt_user_auth_does_not_grant_access(self):
        """Regular IsClient JWT must NOT bypass IsServiceAccount.
        Authenticated via JWT but missing X-Service-Token → 403 (the
        request IS authenticated, just not authorized for this endpoint)."""
        u = User.objects.create_user(
            username="real-client", password="x", role="client",
            phone="+79991110000",
        )
        c = APIClient()
        c.force_authenticate(user=u)
        resp = c.post(
            INTERNAL_URL,
            {"image": _upload()},
            format="multipart",
            HTTP_X_EXTERNAL_USER_ID="bot:12345",
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_empty_settings_token_fails_closed(self, settings):
        """If NUTRITION_SERVICE_TOKEN is unset (misconfigured deployment),
        all requests must be rejected — including with empty header."""
        settings.NUTRITION_SERVICE_TOKEN = ""
        c = APIClient()
        resp = _post(c, token="", external_id="bot:12345")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# External user ID resolution
# ---------------------------------------------------------------------------


class TestExternalUserResolution:
    def test_first_call_creates_proxy_user(self):
        c = APIClient()
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(), primary_provider_name="openai",
        )
        assert not User.objects.filter(username="bot:55555").exists()

        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ), patch(
            "nutrition.views.NutritionLookup",
            return_value=MagicMock(lookup=lambda *a, **kw: None),
        ):
            resp = _post(c, token=SERVICE_TOKEN, external_id="bot:55555")

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        proxy = User.objects.get(username="bot:55555")
        assert proxy.is_proxy is True
        assert proxy.role == "client"
        assert proxy.is_guest is False

    def test_second_call_reuses_existing_proxy(self):
        existing = User.objects.create(
            username="bot:99999", role="client", is_proxy=True,
        )
        c = APIClient()
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(), primary_provider_name="openai",
        )

        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ), patch(
            "nutrition.views.NutritionLookup",
            return_value=MagicMock(lookup=lambda *a, **kw: None),
        ):
            resp = _post(c, token=SERVICE_TOKEN, external_id="bot:99999")

        assert resp.status_code == status.HTTP_200_OK
        # Only one user with this username — get_or_create reused it.
        assert User.objects.filter(username="bot:99999").count() == 1
        scan = FoodScan.objects.get()
        assert scan.user_id == existing.id

    def test_invalid_external_id_returns_400(self):
        c = APIClient()
        # Missing colon → not matching pattern.
        resp = _post(c, token=SERVICE_TOKEN, external_id="just-a-string")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_external_id_returns_400(self):
        c = APIClient()
        resp = c.post(
            INTERNAL_URL,
            {"image": _upload()},
            format="multipart",
            HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Happy path — scan record stored against ProxyUser
# ---------------------------------------------------------------------------


class TestScanFlow:
    def test_successful_scan_persists_against_proxy_user(self):
        c = APIClient()
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(), primary_provider_name="openai",
        )

        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ), patch(
            "nutrition.views.NutritionLookup",
            return_value=MagicMock(lookup=lambda *a, **kw: None),
        ):
            resp = _post(c, token=SERVICE_TOKEN, external_id="bot:42")

        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert body["dish_name"] == "Борщ"
        assert body["confidence"] == 0.9
        scan = FoodScan.objects.get()
        proxy = User.objects.get(username="bot:42")
        assert scan.user_id == proxy.id

    def test_localize_first_scan_row_exists_before_vendor_call(self):
        """152-ФЗ ст. 18 п. 5 — pre-pilot legal gate.

        The FoodScan row tying ``user`` to the uploaded image MUST be
        committed to the RF DB BEFORE the photo bytes are sent across
        the border via OpenAI/Yandex Vision. This test pins the order
        by inspecting the DB row count from inside the router mock —
        when router.scan is called, exactly one FoodScan row must
        already exist for the actor (created by the localize-first
        save call).
        """
        c = APIClient()
        observed_rows_at_router_call: list[int] = []

        def _record_row_count(*args, **kwargs):
            # Snapshot of FoodScan rows for this proxy at the moment
            # OpenAI is being called. If localize-first is removed,
            # this list stays at 0 and the assertion below fails.
            user = User.objects.get(username="bot:loc")
            observed_rows_at_router_call.append(
                FoodScan.objects.filter(user=user).count()
            )
            return RouterResult(
                result=_scan_result(), primary_provider_name="openai",
            )

        router_mock = MagicMock()
        router_mock.scan.side_effect = _record_row_count

        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ), patch(
            "nutrition.views.NutritionLookup",
            return_value=MagicMock(lookup=lambda *a, **kw: None),
        ):
            resp = _post(c, token=SERVICE_TOKEN, external_id="bot:loc")

        assert resp.status_code == status.HTTP_200_OK
        assert observed_rows_at_router_call == [1], (
            "152-ФЗ localize-first invariant broken: FoodScan row "
            "did not exist in RF DB before the OpenAI Vision call. "
            "Move scan.save() BEFORE router.scan() in "
            "nutrition/views.py::InternalFoodScanView.post."
        )
