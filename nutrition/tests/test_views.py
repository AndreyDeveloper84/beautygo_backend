"""Integration tests for POST /api/v1/nutrition/scan/."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import ProviderUnavailable, ScanResult
from nutrition.services.food_scanner_router import (
    AllProvidersFailedError,
    RouterResult,
)


pytestmark = pytest.mark.django_db


SCAN_URL = "/api/v1/nutrition/scan/"


@pytest.fixture
def client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="nut-client", password="x", role="client",
        phone="+79991110000",
    )
    Profile.objects.filter(user=u).update(full_name="Test", city="Penza")
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _image_bytes() -> bytes:
    """Real 4x4 JPEG built via Pillow — DRF's ImageField runs the bytes
    through Pillow.verify() and rejects hand-crafted minimal headers."""
    import io
    from PIL import Image

    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _upload(name="meal.jpg", content_type="image/jpeg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _image_bytes(), content_type=content_type)


def _scan_result(dish="Борщ", confidence=0.9):
    return ScanResult(
        dish_name=dish,
        confidence=confidence,
        portion_g=300,
        ingredients=["свёкла", "капуста"],
        provider="openai",
        latency_ms=400,
        raw_response={"ok": True},
    )


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(SCAN_URL, {"image": _upload()}, format="multipart")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(SCAN_URL, {"image": _upload()}, format="multipart")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_specialist_role_blocked(self):
        from users.models import User

        spec = User.objects.create_user(
            username="nut-spec", password="x", role="specialist",
            phone="+79992220000",
        )
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        c.force_authenticate(user=spec)
        resp = c.post(SCAN_URL, {"image": _upload()}, format="multipart")
        # IsClient is also required; pro app would 403 first.
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_image_returns_400(self, auth_client):
        resp = auth_client.post(SCAN_URL, {}, format="multipart")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_oversized_image_returns_400(self, auth_client):
        big = SimpleUploadedFile(
            "big.jpg", b"\xff\xd8\xff" + b"\x00" * (11 * 1024 * 1024),
            content_type="image/jpeg",
        )
        resp = auth_client.post(SCAN_URL, {"image": big}, format="multipart")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_portion_multiplier_out_of_range_rejected(self, auth_client):
        resp = auth_client.post(
            SCAN_URL,
            {"image": _upload(), "portion_multiplier": 100},
            format="multipart",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestSuccess:
    def test_creates_scan_row_and_returns_envelope(self, auth_client, client_user):
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(),
            primary_provider_name="openai",
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL,
                {"image": _upload(), "portion_multiplier": 1.0},
                format="multipart",
            )

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["dish_name"] == "Борщ"
        assert body["confidence"] == 0.9
        assert body["portion_g"] == 300
        assert body["provider"] == "openai"
        assert body["nutrition"] is None  # Slice 3 will populate

        scan = FoodScan.objects.get(id=body["scan_id"])
        assert scan.user_id == client_user.id
        assert scan.provider_used == "openai"
        assert scan.provider_fallback_from == ""

    def test_fallback_records_primary_in_provider_fallback_from(
        self, auth_client,
    ):
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(),
            primary_provider_name="openai",
            primary_failed_with="ProviderTimeout",
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        scan = FoodScan.objects.get(id=resp.json()["data"]["scan_id"])
        assert scan.provider_fallback_from == "openai"

    def test_portion_multiplier_passed_to_router(self, auth_client):
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(),
            primary_provider_name="openai",
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            auth_client.post(
                SCAN_URL,
                {"image": _upload(), "portion_multiplier": 1.5},
                format="multipart",
            )
        kwargs = router_mock.scan.call_args.kwargs
        assert kwargs["portion_multiplier"] == 1.5


# ---------------------------------------------------------------------------
# All providers failed
# ---------------------------------------------------------------------------


class TestAllProvidersFailed:
    def test_returns_503_and_persists_error_row(self, auth_client, client_user):
        router_mock = MagicMock()
        router_mock.scan.side_effect = AllProvidersFailedError(
            ProviderUnavailable("openai 503"),
            ProviderUnavailable("yandex 503"),
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert resp.json()["error"]["code"] == "FOOD_API_UNAVAILABLE"

        # Audit row stored even on failure — for debugging + cost attribution.
        failed = FoodScan.objects.filter(
            user=client_user, error_code="FOOD_API_UNAVAILABLE",
        )
        assert failed.count() == 1
