"""DRF-1837 — оценка блюда по тексту без записи; происхождение записи (§136).

§109 владельца (10.09.2026): оценка показывается («Я распознала так»,
«примерно»), в дневник попадает только после подтверждения. До этой
правки у каталога была ручная запись по ``dish_name``, которая ПИШЕТ
сразу, — показать оценку до записи было нечем. Здесь:

1. ``internal/food-estimate/`` — та же ``NutritionLookup`` на те же граммы,
   и НИ ОДНОЙ строки в базе (ни FoodLog, ни FoodScan, ни прокси-пользователя).
2. ``entry_origin`` — четыре значения §136 дословно; пишется как передано.
3. ``meal_type="other"`` — «не указан»: бот шлёт его с фото-пути с мая,
   а каталог отвечал 400, и запись из карточки скана не проходила.
"""
from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, FoodScan
from users.models import User

pytestmark = pytest.mark.django_db

ESTIMATE_URL = "/api/v1/nutrition/internal/food-estimate/"
LOG_URL = "/api/v1/nutrition/internal/food-log/"
SERVICE_TOKEN = "test-token-DRF-1837"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def client():
    c = APIClient()
    c.credentials(HTTP_X_SERVICE_TOKEN=SERVICE_TOKEN)
    return c


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:1837", role="client", is_proxy=True)


class TestFoodEstimate:
    def test_requires_service_token(self):
        resp = APIClient().post(ESTIMATE_URL, {"dish_name": "борщ"}, format="json")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_named_grams_scale_the_estimate(self, client):
        resp = client.post(ESTIMATE_URL, {"dish_name": "борщ", "portion_g": 300}, format="json")
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        data = resp.json()["data"]
        assert data["matched_dish"] == "борщ"
        assert data["portion_g"] == 300
        assert data["portion_estimated"] is False
        assert data["kcal"] == pytest.approx(data["kcal_per_100g"] * 3, rel=0.01)

    def test_no_grams_means_a_100g_estimate_named_as_such(self, client):
        resp = client.post(ESTIMATE_URL, {"dish_name": "омлет"}, format="json")
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        data = resp.json()["data"]
        assert data["portion_g"] == 100
        assert data["portion_estimated"] is True
        assert data["kcal"] == pytest.approx(data["kcal_per_100g"], rel=0.01)

    def test_unknown_dish_gets_an_answer_without_numbers(self, client):
        """DRF-2371 — отказа здесь больше нет.

        §109 шаг 6 разрешает запись только по подтверждению показанной
        оценки: пока оценка отвечала 400, блюдо вне справочника нельзя было
        записать текстом вообще. Теперь оценка есть, а чисел в ней нет.
        """
        resp = client.post(ESTIMATE_URL, {"dish_name": "зыбзик квантовый"}, format="json")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["data"]
        assert data["matched_dish"] == "зыбзик квантовый"
        assert data["kcal"] is None

    def test_estimate_writes_nothing(self, client):
        """§109 шаг 6: до подтверждения число не данные человека."""
        before = (FoodLog.objects.count(), FoodScan.objects.count(), User.objects.count())
        resp = client.post(
            ESTIMATE_URL,
            {"dish_name": "борщ", "portion_g": 250},
            format="json",
            HTTP_X_EXTERNAL_USER_ID="bot:1837-new",
        )
        assert resp.status_code == status.HTTP_200_OK  # POSITIVE: оценка была
        after = (FoodLog.objects.count(), FoodScan.objects.count(), User.objects.count())
        assert after == before


class TestEntryOriginAndMealType:
    def _log(self, client, **body):
        payload = {"dish_name": "борщ", "portion_multiplier": 3.0, "meal_type": "lunch"}
        payload.update(body)
        return client.post(LOG_URL, payload, format="json", HTTP_X_EXTERNAL_USER_ID="bot:1837")

    def test_origin_is_written_as_passed(self, client, proxy_user):
        resp = self._log(client, entry_origin="text_estimated_confirmed")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        assert resp.json()["data"]["entry_origin"] == "text_estimated_confirmed"
        row = FoodLog.objects.get(id=resp.json()["data"]["id"])
        assert row.entry_origin == FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED

    def test_all_four_owner_codes_and_nothing_else(self):
        assert set(FoodLog.EntryOrigin.values) == {
            "text_estimated_confirmed",
            "text_user_corrected",
            "photo_estimated_confirmed",
            "photo_user_corrected",
        }

    def test_unknown_origin_is_refused(self, client, proxy_user):
        resp = self._log(client, entry_origin="guessed")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_legacy_call_without_origin_stores_null(self, client, proxy_user):
        resp = self._log(client)
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        assert resp.json()["data"]["entry_origin"] is None

    def test_meal_type_other_is_accepted(self, client, proxy_user):
        """Фото-путь бота шлёт ``other`` с #86 — до DRF-1837 это был 400."""
        resp = self._log(client, meal_type="other")
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        assert resp.json()["data"]["meal_type"] == "other"

    def test_still_refuses_an_invented_meal_type(self, client, proxy_user):
        resp = self._log(client, meal_type="elevenses")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
