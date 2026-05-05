"""Integration tests for POST /api/v1/nutrition/scan/."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodScan
from nutrition.providers.base import ProviderUnavailable, ScanResult
from nutrition.serializers import FoodScanResponseSerializer
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
        # Per spec v2.0 §FOOD SCANNER: nutrition response shape is
        # {calories, protein_g, fat_g, carbs_g, vitamins}. Internal
        # storage on FoodScan.nutrition is richer (per-100g, source,
        # matched_dish) for analytics and Slice 3b derivation.
        # DRF-260 enriched борщ with micronutrients from Скурихин,
        # so vitamins is no longer empty for this dish — assert the
        # macros and the presence of vitamin keys, not the exact map.
        nutrition = body["nutrition"]
        assert nutrition["calories"] == 147.0       # 49 kcal/100g × 300g
        assert nutrition["protein_g"] == pytest.approx(4.8, rel=1e-2)
        assert nutrition["fat_g"] == pytest.approx(6.6, rel=1e-2)
        assert nutrition["carbs_g"] == pytest.approx(20.1, rel=1e-2)
        # Vitamins map is sparse (DRF-264) — only non-null keys land.
        # Borscht seed populates iron, fiber, vit C, calcium, magnesium.
        vitamins = nutrition["vitamins"]
        assert vitamins.get("iron_mg") is not None
        assert vitamins.get("fiber_g") is not None

        scan = FoodScan.objects.get(id=body["scan_id"])
        assert scan.user_id == client_user.id
        assert scan.provider_used == "openai"
        assert scan.provider_fallback_from == ""
        # DB still keeps the rich internal shape with matched_dish.
        assert scan.nutrition["matched_dish"] == "борщ"
        assert scan.nutrition["source"] == "seed_ru"

    def test_nutrition_null_when_all_totals_are_null(self, auth_client, client_user):
        # If lookup matched a dish but per-portion totals are null
        # (provider didn't estimate portion_g), serializer collapses to
        # null instead of {calories: null, protein_g: null, ...} so the
        # mobile UI cleanly prompts manual entry.
        scan = FoodScan.objects.create(
            user=client_user, dish_name="борщ",
            confidence=0.9, portion_g=None,
            provider_used=FoodScan.Provider.OPENAI,
            nutrition={
                "matched_dish": "борщ", "source": "seed_ru",
                "portion_g": None, "kcal_per_100g": 49,
                "protein_g_per_100g": 1.6, "fat_g_per_100g": 2.2,
                "carbs_g_per_100g": 6.7,
                "kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None,
            },
        )
        body = FoodScanResponseSerializer(scan).data
        assert body["nutrition"] is None

    def test_nutrition_null_when_dish_not_in_seed(self, auth_client):
        # Provider returns a dish we have no entry for → nutrition stays
        # null, scan still 200 (mobile shows "уточните вручную").
        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=_scan_result(dish="суши с лососем"),
            primary_provider_name="openai",
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"]["nutrition"] is None

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
    def test_unavailable_returns_503_and_persists_error_row(
        self, auth_client, client_user,
    ):
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
        assert FoodScan.objects.filter(
            user=client_user, error_code="FOOD_API_UNAVAILABLE",
        ).count() == 1

    def test_all_low_confidence_returns_400_food_not_recognized(
        self, auth_client, client_user,
    ):
        # Per spec v2.0 §FOOD SCANNER: vendor worked but image isn't
        # recognisable → 400 FOOD_NOT_RECOGNIZED, not 503.
        from nutrition.providers.base import LowConfidenceError, ScanResult

        partial = ScanResult(
            dish_name="??", confidence=0.2, portion_g=None,
            ingredients=[], provider="openai", latency_ms=300,
        )
        router_mock = MagicMock()
        router_mock.scan.side_effect = AllProvidersFailedError(
            LowConfidenceError("openai 0.2", partial=partial),
            LowConfidenceError("yandex 0.3", partial=partial),
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "FOOD_NOT_RECOGNIZED"
        assert FoodScan.objects.filter(
            user=client_user, error_code="FOOD_NOT_RECOGNIZED",
        ).count() == 1

    def test_mixed_low_confidence_and_timeout_returns_503(
        self, auth_client, client_user,
    ):
        from nutrition.providers.base import LowConfidenceError, ProviderTimeout

        router_mock = MagicMock()
        router_mock.scan.side_effect = AllProvidersFailedError(
            LowConfidenceError("openai 0.2"),
            ProviderTimeout("yandex slow"),
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        # Mixed = we can't say "image not recognisable", default to 503.
        assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


class TestResponseShape:
    def test_includes_beauty_insights_field_as_null_in_mvp(self, auth_client):
        from nutrition.providers.base import ScanResult
        from nutrition.services.food_scanner_router import RouterResult

        router_mock = MagicMock()
        router_mock.scan.return_value = RouterResult(
            result=ScanResult(
                dish_name="Борщ", confidence=0.9, portion_g=300,
                ingredients=[], provider="openai", latency_ms=400,
            ),
            primary_provider_name="openai",
        )
        with patch(
            "nutrition.views.FoodScannerRouter",
            return_value=router_mock,
        ):
            resp = auth_client.post(
                SCAN_URL, {"image": _upload()}, format="multipart",
            )
        body = resp.json()["data"]
        assert "beauty_insights" in body
        assert body["beauty_insights"] is None
