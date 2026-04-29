"""Tests for POST /api/v1/nutrition/food-log/ + FoodLogService.

Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION ``POST /nutrition/food-log``:

Request:
    scan_id?       : UUID
    dish_name?     : string
    portion_multiplier : number (1.0 = standard)
    meal_type      : "breakfast" | "lunch" | "dinner" | "snack"
    logged_at?     : datetime (default now())

Response 201 (FoodLogEntry):
    id, dish_name, calories, protein_g, fat_g, carbs_g, meal_type, logged_at

Two creation paths covered:
- scan_id path — derives macros from FoodScan.nutrition snapshot
- manual dish_name path — derives via NutritionLookup at 100g baseline
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, FoodScan
from nutrition.services.food_log_service import (
    CreateFoodLogInput,
    DishNotRecognizedError,
    FoodLogService,
    InvalidInputError,
    ScanNotOwnedError,
)


pytestmark = pytest.mark.django_db


URL = "/api/v1/nutrition/food-log/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="log-client", password="x", role="client",
        phone="+79993330000",
    )
    Profile.objects.filter(user=u).update(full_name="Log User", city="Penza")
    return u


@pytest.fixture
def other_client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="log-other", password="x", role="client",
        phone="+79993330001",
    )
    Profile.objects.filter(user=u).update(full_name="Other", city="Penza")
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


@pytest.fixture
def borscht_scan(client_user):
    """A scan whose nutrition snapshot matches Slice 3a borscht seed."""
    return FoodScan.objects.create(
        user=client_user,
        dish_name="Борщ",
        confidence=0.9,
        portion_g=300,
        ingredients=["свёкла"],
        provider_used=FoodScan.Provider.OPENAI,
        nutrition={
            "matched_dish": "борщ",
            "source": "seed_ru",
            "portion_g": 300,
            "kcal_per_100g": 49,
            "protein_g_per_100g": 1.6,
            "fat_g_per_100g": 2.2,
            "carbs_g_per_100g": 6.7,
            "kcal": 147.0,        # 49 × 3
            "protein_g": 4.8,
            "fat_g": 6.6,
            "carbs_g": 20.1,
        },
    )


# ---------------------------------------------------------------------------
# Auth + app-type
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(URL, {
            "dish_name": "борщ", "portion_multiplier": 1.0,
            "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(URL, {
            "dish_name": "борщ", "portion_multiplier": 1.0,
            "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Validation (serializer-level)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_neither_scan_nor_dish_returns_400(self, auth_client):
        resp = auth_client.post(URL, {
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_meal_type_returns_400(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "борщ", "portion_multiplier": 1.0,
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_meal_type_returns_400(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "борщ", "portion_multiplier": 1.0,
            "meal_type": "elevenses",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_zero_multiplier_returns_400(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "борщ", "portion_multiplier": 0,
            "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_blank_dish_name_with_no_scan_returns_400(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "", "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# scan_id path
# ---------------------------------------------------------------------------


class TestScanPath:
    def test_creates_log_from_scan_default_multiplier(
        self, auth_client, client_user, borscht_scan,
    ):
        resp = auth_client.post(URL, {
            "scan_id": str(borscht_scan.id),
            "portion_multiplier": 1.0,
            "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        # Spec FoodLogEntry shape
        assert set(body.keys()) == {
            "id", "dish_name", "calories", "protein_g",
            "fat_g", "carbs_g", "meal_type", "logged_at",
        }
        assert body["dish_name"] == "Борщ"
        assert body["calories"] == 147.0
        assert body["meal_type"] == "lunch"

        log = FoodLog.objects.get(id=body["id"])
        assert log.user_id == client_user.id
        assert log.scan_id == borscht_scan.id
        assert log.portion_multiplier == 1.0

    def test_multiplier_2x_doubles_macros(
        self, auth_client, borscht_scan,
    ):
        resp = auth_client.post(URL, {
            "scan_id": str(borscht_scan.id),
            "portion_multiplier": 2.0,
            "meal_type": "dinner",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        body = resp.json()["data"]
        assert body["calories"] == 294.0  # 147 × 2
        assert body["protein_g"] == pytest.approx(9.6, rel=1e-2)

    def test_other_users_scan_returns_404(
        self, auth_client, other_client_user,
    ):
        # Scan owned by someone else — return 404 to avoid existence leak.
        foreign_scan = FoodScan.objects.create(
            user=other_client_user,
            dish_name="Борщ", confidence=0.9, portion_g=300,
            provider_used=FoodScan.Provider.OPENAI,
            nutrition={"kcal": 147.0, "protein_g": 4.8, "fat_g": 6.6, "carbs_g": 20.1},
        )
        resp = auth_client.post(URL, {
            "scan_id": str(foreign_scan.id),
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error"]["code"] == "SCAN_NOT_FOUND"

    def test_scan_with_null_nutrition_returns_food_not_recognized(
        self, auth_client, client_user,
    ):
        scan = FoodScan.objects.create(
            user=client_user, dish_name="суши",
            confidence=0.9, portion_g=200,
            provider_used=FoodScan.Provider.OPENAI,
            nutrition=None,  # Slice 3a missed; mobile prompted manual entry
        )
        resp = auth_client.post(URL, {
            "scan_id": str(scan.id),
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "FOOD_NOT_RECOGNIZED"

    def test_logged_at_passed_through(
        self, auth_client, borscht_scan,
    ):
        when = "2026-04-29T08:30:00Z"
        resp = auth_client.post(URL, {
            "scan_id": str(borscht_scan.id),
            "portion_multiplier": 1.0, "meal_type": "breakfast",
            "logged_at": when,
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        log = FoodLog.objects.get(id=resp.json()["data"]["id"])
        assert log.logged_at == datetime(2026, 4, 29, 8, 30, tzinfo=dt_tz.utc)


# ---------------------------------------------------------------------------
# Manual dish_name path
# ---------------------------------------------------------------------------


class TestManualPath:
    def test_creates_log_from_seed_dish(self, auth_client, client_user):
        # Borscht seed: 49 kcal/100g. multiplier=1.0 → 49 kcal (100g baseline)
        resp = auth_client.post(URL, {
            "dish_name": "борщ",
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert body["dish_name"] == "борщ"
        assert body["calories"] == 49.0

        log = FoodLog.objects.get(id=body["id"])
        assert log.user_id == client_user.id
        assert log.scan is None  # manual entry

    def test_multiplier_3x_borscht_baseline_100g(self, auth_client):
        # multiplier=3.0 against 100g baseline = 300g of borscht = 147 kcal
        resp = auth_client.post(URL, {
            "dish_name": "борщ",
            "portion_multiplier": 3.0, "meal_type": "dinner",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["data"]["calories"] == 147.0

    def test_alias_resolves_via_lookup(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "украинский борщ",  # alias → борщ
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        log = FoodLog.objects.get(id=resp.json()["data"]["id"])
        assert log.dish_name == "борщ"

    def test_unknown_dish_returns_food_not_recognized(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "ризотто с трюфелем",
            "portion_multiplier": 1.0, "meal_type": "dinner",
        }, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "FOOD_NOT_RECOGNIZED"


# ---------------------------------------------------------------------------
# scan_id wins over dish_name
# ---------------------------------------------------------------------------


class TestPriority:
    def test_scan_id_wins_when_both_provided(
        self, auth_client, borscht_scan,
    ):
        # If both scan_id and dish_name come in, scan is more authoritative
        # (provider already saw the photo).
        resp = auth_client.post(URL, {
            "scan_id": str(borscht_scan.id),
            "dish_name": "плов",  # would resolve via lookup but should be ignored
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")
        assert resp.status_code == status.HTTP_201_CREATED
        body = resp.json()["data"]
        assert body["dish_name"] == "Борщ"  # from scan, not "плов"


# ---------------------------------------------------------------------------
# Service unit tests (DI-clean)
# ---------------------------------------------------------------------------


class TestServiceUnit:
    def test_invalid_input_when_neither_field(self, client_user):
        with pytest.raises(InvalidInputError):
            FoodLogService().create(CreateFoodLogInput(
                user_id=client_user.id,
                portion_multiplier=1.0,
                meal_type="lunch",
            ))

    def test_dish_not_recognized_when_lookup_fails(self, client_user):
        with pytest.raises(DishNotRecognizedError):
            FoodLogService().create(CreateFoodLogInput(
                user_id=client_user.id,
                dish_name="неизвестное_блюдо_xyz",
                portion_multiplier=1.0,
                meal_type="lunch",
            ))

    def test_scan_not_owned_raises(self, client_user, other_client_user):
        scan = FoodScan.objects.create(
            user=other_client_user, dish_name="x",
            confidence=0.9, portion_g=100,
            provider_used=FoodScan.Provider.OPENAI,
            nutrition={"kcal": 100, "protein_g": 1, "fat_g": 1, "carbs_g": 1},
        )
        with pytest.raises(ScanNotOwnedError):
            FoodLogService().create(CreateFoodLogInput(
                user_id=client_user.id,
                scan_id=scan.id,
                portion_multiplier=1.0,
                meal_type="lunch",
            ))
