"""«Не посчитано» — это не ноль, и это не отказ записи (DRF-2371).

До этого листа каталог отвечал **отказом** (400 ``FOOD_NOT_RECOGNIZED``),
если числа вывести неоткуда: порция неизвестна или блюда нет в справочнике.
Человек, съевший ризотто с трюфелем, не мог записать его вообще.

Решение владельца (§77 п. 34, вариант «а»): запись сохраняется всегда,
а макросы становятся **отсутствующими**. Отсутствие не равно нулю: ноль
означал бы «съел и не получил калорий».

Отсюда обязательства, которые держат узлы ниже:

* запись ложится и без чисел — по скану без порции и по названию вне
  справочника; в ответе на месте калорий ``null``;
* оценка до записи (§109 шаг 6 — «запись только по подтверждению
  показанной оценки») тоже перестаёт быть отказом, иначе текстовый путь
  до записи не доходит;
* дневной итог не выдаёт частичную сумму за полную: он называет, сколько
  записей осталось без расчёта;
* комментарий ИИ при незасчитанных записях не запрашивается вовсе —
  молчание честнее уверенного вывода о неполном числе;
* «уложился в цель» не засчитывает день, в котором часть съеденного не
  посчитана;
* снимок избранного с такой записи несёт отсутствие, а не ноль.

Подмена для проверки узлов: вернуть в ``FoodLog`` ``default=0.0`` без
``null=True`` — k1/k2 покраснеют на нуле, которого никто не считал.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, FoodScan
from nutrition.services.food_log_service import CreateFoodLogInput, FoodLogService


pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/food-log/"


@pytest.fixture
def client_user(db):
    from users.models import Profile, User

    u = User.objects.create_user(
        username="gap-2371", password="x", role="client", phone="+79993334371",
    )
    Profile.objects.filter(user=u).update(full_name="Gap User", city="Penza")
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


@pytest.fixture
def scan_without_portion(client_user):
    """Провайдер назвал блюдо, но веса не оценил: на порцию чисел нет."""
    return FoodScan.objects.create(
        user=client_user,
        dish_name="Ризотто с трюфелем",
        confidence=0.81,
        portion_g=None,
        provider_used=FoodScan.Provider.OPENAI,
        nutrition={
            "matched_dish": "ризотто",
            "source": "seed_ru",
            "portion_g": None,
            "kcal_per_100g": 180,
            "protein_g_per_100g": 4.0,
            "fat_g_per_100g": 7.0,
            "carbs_g_per_100g": 24.0,
            "kcal": None,
            "protein_g": None,
            "fat_g": None,
            "carbs_g": None,
        },
    )


class TestK1ScanWithoutPortionIsStillLogged:
    def test_the_entry_is_created_and_its_numbers_are_absent(
        self, auth_client, client_user, scan_without_portion,
    ):
        resp = auth_client.post(URL, {
            "scan_id": str(scan_without_portion.id),
            "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")

        # Сначала о наличии: запись создана и названа.
        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()["data"]
        assert data["dish_name"] == "Ризотто с трюфелем"
        log = FoodLog.objects.get(id=data["id"])
        assert log.user_id == client_user.id
        # И только теперь об отсутствии: числа отсутствуют, а не равны нулю.
        assert data["calories"] is None
        assert log.calories is None
        assert log.protein_g is None

    def test_a_scan_with_numbers_still_carries_them(self, auth_client, client_user):
        """Положительная пара: посчитанный скан пишет числа, как прежде."""
        scan = FoodScan.objects.create(
            user=client_user, dish_name="Борщ", confidence=0.9, portion_g=300,
            provider_used=FoodScan.Provider.OPENAI,
            nutrition={
                "matched_dish": "борщ", "source": "seed_ru", "portion_g": 300,
                "kcal": 147.0, "protein_g": 4.8, "fat_g": 6.6, "carbs_g": 20.1,
            },
        )
        resp = auth_client.post(URL, {
            "scan_id": str(scan.id), "portion_multiplier": 1.0, "meal_type": "lunch",
        }, format="json")

        assert resp.status_code == status.HTTP_201_CREATED
        assert resp.json()["data"]["calories"] == 147.0


class TestK2DishOutsideTheCatalogIsStillLogged:
    def test_an_unknown_dish_is_logged_under_the_name_the_person_typed(self, auth_client):
        resp = auth_client.post(URL, {
            "dish_name": "ризотто с трюфелем",
            "portion_multiplier": 1.0, "meal_type": "dinner",
        }, format="json")

        assert resp.status_code == status.HTTP_201_CREATED
        data = resp.json()["data"]
        assert data["dish_name"] == "ризотто с трюфелем"
        assert data["calories"] is None


class TestK3EstimateIsNoLongerARefusal:
    """§109 шаг 6: без показанной оценки записи текстом не бывает."""

    def test_an_unknown_dish_gets_an_answer_without_numbers(self, settings):
        settings.NUTRITION_SERVICE_TOKEN = "test-token-DRF-2371"
        c = APIClient()
        c.credentials(HTTP_X_SERVICE_TOKEN="test-token-DRF-2371")
        resp = c.post(
            "/api/v1/nutrition/internal/food-estimate/",
            {"dish_name": "ризотто с трюфелем"},
            format="json",
        )

        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["data"]
        assert data["matched_dish"] == "ризотто с трюфелем"
        assert data["kcal"] is None


class TestK4TheDayTotalDoesNotPassPartialForWhole:
    def test_the_total_counts_what_was_counted_and_names_the_rest(self, client_user):
        from nutrition.services.nutrition_summary_service import NutritionSummaryService

        day = datetime(2026, 9, 24, 12, 0, tzinfo=dt_tz.utc)
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Борщ", portion_multiplier=1.0,
            calories=250.0, protein_g=10.0, fat_g=8.0, carbs_g=30.0,
            meal_type="lunch", logged_at=day,
        )
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Ризотто с трюфелем",
            portion_multiplier=1.0,
            calories=None, protein_g=None, fat_g=None, carbs_g=None,
            meal_type="dinner", logged_at=day + timedelta(hours=6),
        )

        summary = NutritionSummaryService().summary(user_id=client_user.id, day=day.date())

        assert summary.totals.calories == 250.0
        # Частичная сумма, выданная за полную, — та же ложь, что «0 ккал».
        assert summary.totals.unscored_entries == 1

    def test_a_fully_counted_day_reports_no_gap(self, client_user):
        from nutrition.services.nutrition_summary_service import NutritionSummaryService

        day = datetime(2026, 9, 24, 12, 0, tzinfo=dt_tz.utc)
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Борщ", portion_multiplier=1.0,
            calories=250.0, protein_g=10.0, fat_g=8.0, carbs_g=30.0,
            meal_type="lunch", logged_at=day,
        )

        summary = NutritionSummaryService().summary(user_id=client_user.id, day=day.date())

        assert summary.totals.calories == 250.0
        assert summary.totals.unscored_entries == 0


class TestK5TheAICommentStaysSilentOnAPartialDay:
    def test_no_comment_is_requested_while_a_meal_is_uncounted(self, client_user, monkeypatch):
        from nutrition.services import nutrition_summary_service as mod

        day = datetime(2026, 9, 24, 12, 0, tzinfo=dt_tz.utc)
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Ризотто с трюфелем",
            portion_multiplier=1.0, calories=None, protein_g=None,
            fat_g=None, carbs_g=None, meal_type="dinner", logged_at=day,
        )
        called: list[object] = []

        class _Spy:
            def comment_for(self, **kwargs):
                called.append(kwargs)
                return "день собран хорошо"

        monkeypatch.setattr(
            "nutrition.services.ai_comment_service.AICommentService", _Spy,
        )

        summary = mod.NutritionSummaryService().summary(
            user_id=client_user.id, day=day.date(), with_comment=True,
        )

        # Комментарий о неполном числе звучал бы уверенно и был бы неправдой.
        assert called == []
        assert summary.ai_comment is None


class TestK6GoalDaysDoNotCountAGuess:
    def test_a_day_with_an_uncounted_meal_is_not_a_day_within_the_goal(self, client_user):
        from nutrition.models import NutritionProfile
        from nutrition.services.plan_facts import count_days_within_calorie_target

        NutritionProfile.objects.update_or_create(
            user_id=client_user.id,
            defaults={"daily_kcal": 2000, "calories_source": "manual"},
        )
        day = datetime.now(dt_tz.utc) - timedelta(days=1)
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Борщ", portion_multiplier=1.0,
            calories=300.0, protein_g=10.0, fat_g=8.0, carbs_g=30.0,
            meal_type="lunch", logged_at=day,
        )
        FoodLog.objects.create(
            user_id=client_user.id, dish_name="Ризотто с трюфелем",
            portion_multiplier=1.0, calories=None, protein_g=None, fat_g=None,
            carbs_g=None, meal_type="dinner", logged_at=day,
        )

        # 300 ккал «уложились» бы в 2000 — но 300 это не весь день.
        start = datetime.now(dt_tz.utc) - timedelta(days=7)
        end = datetime.now(dt_tz.utc) + timedelta(days=1)
        assert count_days_within_calorie_target(client_user.id, start, end) == 0


class TestK7FavouriteSnapshotCarriesAbsence:
    def test_saving_an_uncounted_entry_keeps_the_numbers_absent(self, client_user):
        from nutrition.services.saved_meal_service import SavedMealService

        log = FoodLog.objects.create(
            user_id=client_user.id, dish_name="Ризотто с трюфелем",
            portion_multiplier=1.0, calories=None, protein_g=None, fat_g=None,
            carbs_g=None, meal_type="dinner",
            logged_at=datetime(2026, 9, 24, 12, 0, tzinfo=dt_tz.utc),
        )

        outcome = SavedMealService().save_from_food_log(client_user, log.id)

        assert outcome.meal.calories is None


class TestK8TheEntryStillPrintsItself:
    def test_str_does_not_break_on_an_uncounted_entry(self, client_user):
        log = FoodLog.objects.create(
            user_id=client_user.id, dish_name="Ризотто с трюфелем",
            portion_multiplier=1.0, calories=None, protein_g=None, fat_g=None,
            carbs_g=None, meal_type="dinner",
            logged_at=datetime(2026, 9, 24, 12, 0, tzinfo=dt_tz.utc),
        )

        # Админка и журналы печатают запись; падать на отсутствии нельзя.
        assert "Ризотто с трюфелем" in str(log)


class TestK9ServiceUnitPaths:
    def test_manual_path_without_a_match_returns_an_entry(self, client_user):
        log = FoodLogService().create(CreateFoodLogInput(
            user_id=client_user.id, portion_multiplier=1.0,
            meal_type="lunch", dish_name="ризотто с трюфелем",
        ))

        assert log.pk is not None
        assert log.calories is None
