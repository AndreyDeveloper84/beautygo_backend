"""DRF-2777 — дневник питания пишется только под основанием ``food_diary_processing``.

Две двери — два правила (``nutrition/services/food_diary_consent.py``):

* клиентская ``/nutrition/food-log/`` — утверждение основания обязательно
  (вид + версия текста), без него ``422 CONSENT_REQUIRED`` и ни строки;
  известный отзыв побеждает и утверждение;
* внутренние пути бота — запись еды, её восстановление, запись воды (она
  пишет зеркало в дневник) и восстановление воды — отказывают только при
  известном отзыве; неизвестное состояние — не отзыв (решение (б) DRF-2776),
  основной сторож у них — бот.

Каждое «не записано» стоит рядом с «а так — записано».
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework.test import APIClient

from nutrition.models import Beverage, DeletedFoodLog, FoodLog, WaterEntry
from users.models import ConsentState, User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-service-token-DRF-2777"  # noqa: S105
EXTERNAL_ID = "bot:max:2777"
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=dt_tz.utc)

CLIENT_URL = "/api/v1/nutrition/food-log/"
INTERNAL_FOOD_URL = "/api/v1/nutrition/internal/food-log/"
INTERNAL_WATER_URL = "/api/v1/nutrition/internal/water/"

MEAL = {"dish_name": "борщ", "portion_multiplier": 1.0, "meal_type": "lunch"}
ATTESTED = {"consent": {"type": "food_diary_processing", "document_version": "food-diary-v1"}}


@pytest.fixture(autouse=True)
def _token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def person(db):
    return User.objects.create(username=EXTERNAL_ID, role="client", is_proxy=True)


def _client_app(person) -> APIClient:
    api = APIClient()
    api.force_authenticate(user=person)
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    return api


def _bot() -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN)
    api.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_ID
    return api


def _state(person, *, granted: bool, at: datetime = T0) -> None:
    """Последнее доставленное ботом состояние ``food_diary_processing``."""
    ConsentState.objects.update_or_create(
        user=person,
        consent_type="food_diary_processing",
        defaults={"granted": granted, "granted_at": at, "event_id": f"ev-{granted}-{at:%H%M}"},
    )


def _refusal(response, reason: str) -> None:
    assert response.status_code == 422, response.content
    error = response.json()["error"]
    assert error["code"] == "CONSENT_REQUIRED"
    assert error["details"] == {"consent_type": "food_diary_processing", "reason": reason}


def _diary(person) -> int:
    return FoodLog.objects.filter(user=person).count()


class TestTheClientAppDoor:
    def test_with_the_basis_the_meal_is_written(self, person):
        response = _client_app(person).post(CLIENT_URL, {**MEAL, **ATTESTED}, format="json")

        assert response.status_code == 201, response.content
        assert _diary(person) == 1

    def test_without_the_basis_nothing_is_written(self, person):
        written = _client_app(person).post(CLIENT_URL, {**MEAL, **ATTESTED}, format="json")
        assert written.status_code == 201, written.content

        response = _client_app(person).post(CLIENT_URL, MEAL, format="json")

        _refusal(response, "not_attested")
        assert _diary(person) == 1

    @pytest.mark.parametrize(
        "consent",
        [
            {"type": "personal_calculation", "document_version": "food-diary-v1"},
            {"type": "food_diary_processing"},
            {"type": "food_diary_processing", "document_version": "  "},
            "food_diary_processing",
        ],
        ids=["another_consent", "no_version", "blank_version", "not_an_object"],
    )
    def test_a_malformed_basis_is_no_basis(self, person, consent):
        response = _client_app(person).post(CLIENT_URL, {**MEAL, "consent": consent}, format="json")

        _refusal(response, "not_attested")
        assert _diary(person) == 0

    def test_a_known_withdrawal_beats_the_basis(self, person):
        _state(person, granted=False)

        response = _client_app(person).post(CLIENT_URL, {**MEAL, **ATTESTED}, format="json")

        _refusal(response, "withdrawn")
        assert _diary(person) == 0

    def test_a_newer_grant_after_the_withdrawal_writes_again(self, person):
        _state(person, granted=False)
        _state(person, granted=True, at=T0 + timedelta(hours=1))

        response = _client_app(person).post(CLIENT_URL, {**MEAL, **ATTESTED}, format="json")

        assert response.status_code == 201, response.content
        assert _diary(person) == 1


class TestTheBotFoodDoors:
    def test_an_unknown_state_is_not_a_withdrawal(self, person):
        response = _bot().post(INTERNAL_FOOD_URL, MEAL, format="json")

        assert response.status_code == 201, response.content
        assert _diary(person) == 1

    def test_a_known_withdrawal_refuses_the_bot_write(self, person):
        _state(person, granted=False)

        response = _bot().post(INTERNAL_FOOD_URL, MEAL, format="json")

        _refusal(response, "withdrawn")
        assert _diary(person) == 0

    def test_a_known_withdrawal_refuses_to_restore_a_meal(self, person):
        created = _bot().post(INTERNAL_FOOD_URL, MEAL, format="json")
        assert created.status_code == 201, created.content
        entry_id = created.json()["data"]["id"]
        deleted = _bot().delete(f"{INTERNAL_FOOD_URL}{entry_id}/")
        assert deleted.status_code in (200, 204), deleted.content
        assert DeletedFoodLog.objects.filter(pk=entry_id).exists()
        _state(person, granted=False)

        response = _bot().post(f"{INTERNAL_FOOD_URL}{entry_id}/restore/")

        _refusal(response, "withdrawn")
        assert DeletedFoodLog.objects.filter(pk=entry_id).exists()
        assert _diary(person) == 0


LATTE = {"ml": 250, "beverage_slug": "latte-2777"}


@pytest.fixture
def latte(db):
    """Калорийный напиток: зеркало в дневник пишется только при kcal > 0."""
    return Beverage.objects.create(
        slug="latte-2777", name_ru="Латте", category="coffee",
        water_coefficient=0.8, kcal_per_100ml=60.0,
    )


class TestTheWaterDoorAndItsMirror:
    def test_an_unknown_state_writes_the_water_and_its_mirror(self, person, latte):
        response = _bot().post(INTERNAL_WATER_URL, LATTE, format="json")

        assert response.status_code == 201, response.content
        assert WaterEntry.objects.filter(user=person).count() == 1
        assert _diary(person) == 1

    def test_a_known_withdrawal_refuses_the_water_and_its_mirror(self, person, latte):
        _state(person, granted=False)

        response = _bot().post(INTERNAL_WATER_URL, LATTE, format="json")

        _refusal(response, "withdrawn")
        assert WaterEntry.objects.filter(user=person).count() == 0
        assert _diary(person) == 0

    def test_a_known_withdrawal_refuses_to_restore_the_water(self, person):
        created = _bot().post(INTERNAL_WATER_URL, {"ml": 250}, format="json")
        assert created.status_code == 201, created.content
        entry_id = WaterEntry.objects.get(user=person).id
        deleted = _bot().delete(f"{INTERNAL_WATER_URL}{entry_id}/")
        assert deleted.status_code in (200, 204), deleted.content
        assert WaterEntry.objects.filter(pk=entry_id, deleted_at__isnull=False).exists()
        _state(person, granted=False)

        response = _bot().post(f"{INTERNAL_WATER_URL}{entry_id}/restore/")

        _refusal(response, "withdrawn")
        assert WaterEntry.objects.filter(pk=entry_id, deleted_at__isnull=False).exists()
