"""DELETE /internal/profile/body-parameters/ — отзыв согласия на персональный
расчёт (пакет владельца 12.09 §2, DRF-1698).

После подтверждения «Отключить и удалить»: шесть параметров стёрты,
ориентиры недоступны (NULL, source=none), история дневника цела.
Идемпотентно; «профиля не было» — тоже 200.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from nutrition.models import FoodLog, NutritionProfile, WaterEntry
from nutrition.services import personal_calculation_withdrawal as svc
from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-1698"  # noqa: S105
PROFILE_URL = "/api/v1/nutrition/internal/profile/"
ERASE_URL = "/api/v1/nutrition/internal/profile/body-parameters/"
CONSENT = {"type": "personal_calculation", "document_version": "v1"}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:1698", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:1698",
    }


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def full_profile(api, proxy_user, headers):
    """Профиль с шестью параметрами и посчитанными ориентирами — через ту
    же ручку, которой пишет бот, а не напрямую в базу."""
    resp = api.post(
        PROFILE_URL,
        {
            "consent": CONSENT,
            "gender": "female",
            "age": 30,
            "height_cm": 165,
            "weight_kg": 60,
            "activity_coefficient": 1.6,
            "goal": "lose",
        },
        format="json",
        **headers,
    )
    assert resp.status_code in (200, 201), resp.content
    p = NutritionProfile.objects.get(user=proxy_user)
    assert p.daily_kcal is not None, "фикстура обязана дать ориентир — иначе тест инвалидации пуст"
    assert p.targets_source == NutritionProfile.TargetsSource.AYLA_CALCULATED
    return p


class TestErasureIsCompleteAndKeepsTheDiary:
    def test_six_inputs_and_targets_are_gone_diary_stays(self, api, headers, full_profile, proxy_user):
        FoodLog.objects.create(
            user=proxy_user, dish_name="Борщ", portion_multiplier=1.0,
            calories=250, protein_g=8, fat_g=6, carbs_g=35, meal_type="lunch",
            logged_at=datetime.now(timezone.utc),
        )
        WaterEntry.objects.create(
            user=proxy_user, ts=datetime.now(timezone.utc), ml=250, water_ml=250.0,
        )

        resp = api.delete(ERASE_URL, **headers)

        assert resp.status_code == 200, resp.content
        data = resp.json()["data"]
        assert data["profile_existed"] is True
        assert data["targets_cleared"] is True
        assert set(data["erased"]) == {
            "weight_kg", "height_cm", "age", "gender", "activity_coefficient", "goal",
        }
        p = NutritionProfile.objects.get(pk=full_profile.pk)
        assert (p.weight_kg, p.height_cm, p.age, p.gender) == (None, None, None, "")
        assert p.activity_coefficient == 1.4
        assert p.goal == ""
        assert p.daily_kcal is None and p.bmr is None and p.daily_water_ml is None
        assert p.targets_source == NutritionProfile.TargetsSource.NONE
        assert p.targets_input_snapshot == {}
        # История дневника — на месте.
        assert FoodLog.objects.filter(user=proxy_user).count() == 1
        assert WaterEntry.objects.filter(user=proxy_user).count() == 1

    def test_untouched_fields_stay(self, api, headers, full_profile):
        full_profile.timezone = "Europe/Samara"
        full_profile.diet_preference = "vegan"
        full_profile.save(update_fields=["timezone", "diet_preference"])

        api.delete(ERASE_URL, **headers)

        p = NutritionProfile.objects.get(pk=full_profile.pk)
        assert p.timezone == "Europe/Samara"
        assert p.diet_preference == "vegan"

    def test_repeat_and_no_profile_are_200(self, api, headers, proxy_user):
        first = api.delete(ERASE_URL, **headers)
        assert first.status_code == 200, first.content
        assert first.json()["data"]["profile_existed"] is False

        NutritionProfile.objects.create(user=proxy_user, weight_kg=70)
        second = api.delete(ERASE_URL, **headers)
        third = api.delete(ERASE_URL, **headers)
        assert second.status_code == third.status_code == 200
        assert third.json()["data"]["targets_cleared"] is False

    def test_the_profile_summary_no_longer_shows_norms(self, api, headers, full_profile):
        """Нормы недоступны — по самой ручке профиля, не по столбцам."""
        before = api.get(PROFILE_URL, **headers).json()["data"]
        assert before["norms"].get("daily_kcal") not in (None, 0)

        api.delete(ERASE_URL, **headers)

        after = api.get(PROFILE_URL, **headers).json()["data"]
        # Блок норм уезжает пустым, когда источника нет (§103) — не нулями.
        assert after["norms"] == {}


class TestIncompleteErasureRollsBack:
    def test_a_write_that_leaves_residue_is_rolled_back_and_500(self, api, headers, full_profile):
        """Полнота проверяется по перечитанной строке ВНУТРИ транзакции."""
        real = svc._strip_purged

        def _leaky(value):
            out = real(value)
            return {**out, "weight_kg": 60} if isinstance(out, dict) else out

        with patch.object(svc, "_strip_purged", _leaky):
            resp = api.delete(ERASE_URL, **headers)

        assert resp.status_code == 500, resp.content
        p = NutritionProfile.objects.get(pk=full_profile.pk)
        # Откат: ни входы, ни ориентиры не тронуты.
        assert p.weight_kg == 60
        assert p.daily_kcal is not None


class TestTheGuard:
    def test_no_service_token_is_refused_and_nothing_erased(self, api, full_profile):
        resp = api.delete(ERASE_URL, HTTP_X_EXTERNAL_USER_ID="bot:1698")
        assert resp.status_code in (401, 403), resp.content
        assert NutritionProfile.objects.get(pk=full_profile.pk).weight_kg == 60

    def test_get_and_post_are_not_served_here(self, api, headers, full_profile):
        assert api.get(ERASE_URL, **headers).status_code == 405
        assert api.post(ERASE_URL, {}, format="json", **headers).status_code == 405
