"""DRF-2776 — каталог применяет смену согласия, доставленную ботом.

Решение владельца 05.10 (D, DRF-2434) и контракт с бот-стороной (окно ayla-8d):

* ``POST /api/v1/internal/me/consent-events/``, Bearer + ``X-External-User-ID``;
  субъект только из заголовка, без создания пользователя;
* ответ всегда ``200`` с ``outcome`` (applied / duplicate / ignored / stale /
  no_subject) и ИМЕНАМИ стёртых полей; ``400`` — форма, ``403`` — авторизация;
* порядок — по ``granted_at``; при равенстве побеждает отзыв; стирание — только
  самым новым отзывом; повторное согласие стёртое не возвращает;
* D-1: отзыв ``personal_calculation`` — полный набор §92 и всё выведенное;
  отзыв ``health`` — флаги и ориентиры, поднятые от них;
* D-2: отзыв ``food_diary_processing`` останавливает бьюти-инсайт.

Стирание необратимо, поэтому каждое утверждение «стёрто» стоит рядом с
положительным контролем «было» и «соседнее — на месте».
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from notifications.models import Notification
from nutrition.models import FoodLog, NutritionProfile
from users.models import ConsentEventReceipt, ConsentState, User

pytestmark = pytest.mark.django_db

TOKEN = "test-token-DRF-2776"  # noqa: S105
URL = "/api/v1/internal/me/consent-events/"
EXTERNAL_ID = "bot:max:2776"
T0 = datetime(2026, 10, 5, 10, 0, tzinfo=dt_tz.utc)


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


@pytest.fixture
def person(db):
    return User.objects.create(username=EXTERNAL_ID, role="client", is_proxy=True)


@pytest.fixture
def profile(person):
    """Всё, что анкета питания знает о теле и здоровье, плюс выведенное из этого."""
    return NutritionProfile.objects.create(
        user=person,
        gender="female", age=30, height_cm=165, weight_kg=60.0, weight_range="55-65",
        activity_coefficient=1.6, goal="lose", pace="moderate", timezone="Europe/Samara",
        health_flags={"pregnant": True},
        bmr=1400, daily_kcal=1800, daily_protein_g=90, daily_fat_g=60, daily_carbs_g=200,
        daily_water_ml=2100, daily_vitamin_d_iu=600, daily_vitamin_b12_mcg=2.6,
        daily_vitamin_c_mg=85, daily_iron_mg=27, daily_calcium_mg=1000, daily_magnesium_mg=350,
        daily_omega3_g=1.4, daily_fiber_g=28,
        targets_source=NutritionProfile.TargetsSource.AYLA_PROPOSED,
        targets_input_snapshot={"weight_kg": 60.0, "height_cm": 165},
        targets_computed_at=T0, targets_confirmed_at=T0,
        goal_overridden_by="bmi_guard", bmi_warning_overridden_at=T0,
        last_overrides_applied=["bmi_low_goal_maintain"],
    )


def _post(body: dict, *, external_id: str | None = EXTERNAL_ID, token: str | None = TOKEN):
    headers = {}
    if token is not None:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if external_id is not None:
        headers["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return APIClient().post(URL, body, format="json", **headers)


def _event(event_id: str, consent_type: str, granted: bool, at: datetime = T0) -> dict:
    return {
        "event_id": event_id, "consent_type": consent_type, "granted": granted,
        "granted_at": at.isoformat(), "granted_via": "bot",
    }


def _data(response) -> dict:
    assert response.status_code == 200, response.content
    return response.json()["data"]


BODY_FIELDS = ("gender", "age", "height_cm", "weight_kg", "weight_range", "activity_coefficient", "goal")


# ── Контракт ───────────────────────────────────────────────────────────────


class TestTheContract:
    def test_without_the_bearer_the_door_is_closed(self, person):
        assert _post(_event("ev-0000001", "marketing", False), token=None).status_code == 403
        assert _post(_event("ev-0000002", "marketing", False), token="wrong").status_code == 403

    def test_without_the_person_header_the_door_is_closed(self, person):
        assert _post(_event("ev-0000003", "marketing", False), external_id=None).status_code == 403

    @pytest.mark.parametrize(
        "broken",
        [
            {"granted": "false"},
            {"granted": 0},
            {"granted_at": "2026-10-05T10:00:00"},
            {"event_id": "short"},
            {"consent_type": ""},
        ],
        ids=["granted-as-string", "granted-as-number", "time-without-zone", "short-event-id", "no-type"],
    )
    def test_a_malformed_body_is_refused_and_nothing_is_recorded(self, person, profile, broken):
        response = _post({**_event("ev-0000004", "personal_calculation", False), **broken})

        assert response.status_code == 400
        assert ConsentEventReceipt.objects.count() == 0
        profile.refresh_from_db()
        assert profile.weight_kg == 60.0

    def test_an_unknown_person_is_a_success_and_no_account_is_created(self, db):
        users_before = User.objects.count()

        data = _data(_post(_event("ev-0000005", "personal_calculation", False), external_id="bot:max:nobody"))

        assert data == {"event_id": "ev-0000005", "outcome": "no_subject", "erased": []}
        assert User.objects.count() == users_before
        assert ConsentEventReceipt.objects.get(event_id="ev-0000005").user_id is None

    def test_a_type_the_catalog_does_not_use_is_a_success_with_a_receipt(self, person):
        data = _data(_post(_event("ev-0000006", "marketing", False)))

        assert data["outcome"] == "ignored"
        assert ConsentEventReceipt.objects.get(event_id="ev-0000006").outcome == "ignored"
        assert not ConsentState.objects.exists()

    def test_the_proxy_bound_to_a_real_account_acts_on_the_real_account(self, db):
        real = User.objects.create(username="user_real_2776", role="client")
        User.objects.create(username=EXTERNAL_ID, role="client", is_proxy=True, linked_user=real)
        NutritionProfile.objects.create(user=real, weight_kg=70.0)

        _data(_post(_event("ev-0000007", "personal_calculation", False)))

        assert NutritionProfile.objects.get(user=real).weight_kg is None


# ── D-1: отзыв personal_calculation ────────────────────────────────────────


class TestWithdrawingThePersonalCalculation:
    def test_the_whole_set_and_everything_derived_from_it_is_erased(self, person, profile):
        data = _data(_post(_event("ev-0000010", "personal_calculation", False)))

        assert data["outcome"] == "applied"
        assert set(BODY_FIELDS) <= set(data["erased"])
        p = NutritionProfile.objects.get(pk=profile.pk)
        assert (p.gender, p.age, p.height_cm, p.weight_kg, p.weight_range, p.activity_coefficient, p.goal) == (
            "", None, None, None, "", None, "",
        )
        assert (p.bmr, p.daily_kcal, p.daily_water_ml, p.daily_iron_mg) == (None, None, None, None)
        assert (p.targets_input_snapshot, p.targets_computed_at, p.targets_confirmed_at) == ({}, None, None)
        assert (p.goal_overridden_by, p.bmi_warning_overridden_at, p.last_overrides_applied) == ("", None, [])

    def test_health_flags_and_the_rest_of_the_profile_stay(self, person, profile):
        FoodLog.objects.create(
            user=person, dish_name="суп", calories=100, protein_g=1, fat_g=1, carbs_g=10,
            meal_type="lunch", logged_at=T0,
        )

        _data(_post(_event("ev-0000011", "personal_calculation", False)))

        p = NutritionProfile.objects.get(pk=profile.pk)
        assert (p.health_flags, p.pace, p.timezone) == ({"pregnant": True}, "moderate", "Europe/Samara")
        assert FoodLog.objects.filter(user=person).count() == 1

    def test_a_grant_records_the_state_and_erases_nothing(self, person, profile):
        data = _data(_post(_event("ev-0000012", "personal_calculation", True)))

        assert (data["outcome"], data["erased"]) == ("applied", [])
        assert NutritionProfile.objects.get(pk=profile.pk).weight_kg == 60.0
        assert ConsentState.objects.get(user=person, consent_type="personal_calculation").granted is True


# ── D-1: отзыв health ─────────────────────────────────────────────────────


class TestWithdrawingHealth:
    def test_the_flags_and_the_targets_raised_by_them_are_erased(self, person, profile):
        data = _data(_post(_event("ev-0000020", "health", False)))

        assert data["outcome"] == "applied"
        p = NutritionProfile.objects.get(pk=profile.pk)
        assert p.health_flags == {}
        assert (p.daily_iron_mg, p.daily_calcium_mg, p.daily_omega3_g, p.daily_fiber_g) == (None, None, None, None)
        assert set(data["erased"]) == {
            "health_flags", "daily_vitamin_d_iu", "daily_vitamin_b12_mcg", "daily_vitamin_c_mg",
            "daily_iron_mg", "daily_calcium_mg", "daily_magnesium_mg", "daily_omega3_g", "daily_fiber_g",
        }

    def test_the_body_and_the_calorie_target_stay(self, person, profile):
        _data(_post(_event("ev-0000021", "health", False)))

        p = NutritionProfile.objects.get(pk=profile.pk)
        assert (p.weight_kg, p.daily_kcal, p.daily_water_ml) == (60.0, 1800, 2100)


# ── Порядок и идемпотентность ─────────────────────────────────────────────


class TestOrderAndRepeats:
    def test_a_repeated_event_is_a_duplicate_and_does_not_erase_again(self, person, profile):
        first = _data(_post(_event("ev-0000030", "personal_calculation", False)))
        NutritionProfile.objects.filter(pk=profile.pk).update(weight_kg=61.0)  # человек ввёл заново

        again = _data(_post(_event("ev-0000030", "personal_calculation", False)))

        assert again == {**first, "outcome": "duplicate"}
        assert NutritionProfile.objects.get(pk=profile.pk).weight_kg == 61.0
        assert ConsentEventReceipt.objects.filter(event_id="ev-0000030").count() == 1

    def test_a_late_withdrawal_older_than_a_newer_grant_is_stale_and_erases_nothing(self, person, profile):
        _data(_post(_event("ev-0000031", "personal_calculation", True, at=T0 + timedelta(hours=1))))

        data = _data(_post(_event("ev-0000032", "personal_calculation", False, at=T0)))

        assert (data["outcome"], data["erased"]) == ("stale", [])
        assert NutritionProfile.objects.get(pk=profile.pk).weight_kg == 60.0
        assert ConsentState.objects.get(user=person, consent_type="personal_calculation").granted is True

    def test_the_newest_withdrawal_erases(self, person, profile):
        _data(_post(_event("ev-0000033", "personal_calculation", True, at=T0)))

        data = _data(_post(_event("ev-0000034", "personal_calculation", False, at=T0 + timedelta(hours=1))))

        assert data["outcome"] == "applied"
        assert NutritionProfile.objects.get(pk=profile.pk).weight_kg is None

    def test_at_the_same_moment_the_withdrawal_wins_in_either_order(self, person, profile):
        _data(_post(_event("ev-0000035", "personal_calculation", True, at=T0)))
        revoke = _data(_post(_event("ev-0000036", "personal_calculation", False, at=T0)))
        grant_again = _data(_post(_event("ev-0000037", "personal_calculation", True, at=T0)))

        assert (revoke["outcome"], grant_again["outcome"]) == ("applied", "stale")
        assert ConsentState.objects.get(user=person, consent_type="personal_calculation").granted is False

    def test_a_new_grant_does_not_bring_erased_data_back(self, person, profile):
        _data(_post(_event("ev-0000038", "personal_calculation", False, at=T0)))

        data = _data(_post(_event("ev-0000039", "personal_calculation", True, at=T0 + timedelta(hours=1))))

        assert data["outcome"] == "applied"
        assert NutritionProfile.objects.get(pk=profile.pk).weight_kg is None


# ── D-2: бьюти-инсайт ─────────────────────────────────────────────────────


class TestTheBeautyInsightHonoursTheFoodDiaryConsent:
    @pytest.fixture(autouse=True)
    def _no_celery(self):
        with patch("notifications.tasks.deliver_notification.delay"):
            yield

    def _active(self, user) -> None:
        FoodLog.objects.create(
            user=user, dish_name="борщ", calories=147, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
            meal_type="lunch", logged_at=datetime.now(dt_tz.utc),
        )

    def test_one_who_withdrew_gets_no_insight_and_another_still_does(self, person):
        from notifications.tasks import dispatch_beauty_insights

        other = User.objects.create(username="bot:max:2776-other", role="client", is_proxy=True)
        self._active(person)
        self._active(other)
        _data(_post(_event("ev-0000040", "food_diary_processing", False)))

        dispatch_beauty_insights()

        sent_to = set(Notification.objects.filter(template_id="beauty_insight").values_list("user_id", flat=True))
        assert sent_to == {other.id}

    def test_an_unknown_state_is_not_a_withdrawal(self, person):
        """Решение (б): неизвестное — не отзыв; дневник пишется под этим согласием."""
        from notifications.tasks import dispatch_beauty_insights

        self._active(person)

        assert dispatch_beauty_insights()["queued"] == 1

    def test_a_newer_grant_after_a_withdrawal_resumes_the_insight(self, person):
        from notifications.tasks import dispatch_beauty_insights

        self._active(person)
        _data(_post(_event("ev-0000041", "food_diary_processing", False, at=T0)))
        _data(_post(_event("ev-0000042", "food_diary_processing", True, at=T0 + timedelta(hours=1))))

        assert dispatch_beauty_insights()["queued"] == 1


# ── Удаление человека ─────────────────────────────────────────────────────


class TestTheErasureOfAPersonKnowsTheNewPointers:
    def test_both_new_pointers_are_decided(self):
        from users.deletion_executor import undecided_pointers

        assert undecided_pointers() == {}

    def test_the_three_entries_of_the_personal_calculation_share_one_eraser(self):
        """Ручка «Отключить и удалить», исполнитель удаления и «забыть всё» —
        и теперь событие от бота — стирают одной функцией: починка её дыр
        (weight_range, выведенное из ИМТ) закрывает их на всех входах."""
        import inspect

        from nutrition import views
        from users import consent_events, deletion_executor, forget_all_catalog

        for module in (views, deletion_executor, forget_all_catalog, consent_events):
            assert "erase_personal_calculation_inputs" in inspect.getsource(module), module.__name__


# ── Предел D-2: пути записи дневника мимо согласия (DRF-2777) ──────────────


class TestEveryFoodLogWriteRequiresTheFoodDiaryConsent:
    """Инвариант, на котором держится решение (б): запись в дневник идёт только
    под ``food_diary_processing``. Сегодня каталог это согласие не проверяет ни
    на одном пути — бот-путь защищён ботом, а клиентская ручка нет. Узел
    стоит честно красным (xfail, strict) до DRF-2777: когда проверку введут, он
    станет зелёным и xfail придётся снять."""

    WRITE = {"dish_name": "борщ", "portion_multiplier": 1.0, "meal_type": "lunch"}

    def _client(self, person) -> APIClient:
        api = APIClient()
        api.force_authenticate(user=person)
        api.defaults["HTTP_X_APP_TYPE"] = "client"
        return api

    def test_control_the_same_request_writes_a_food_log_today(self, person):
        """Контроль, что xfail ниже красный ПО НУЖНОЙ причине: запрос валиден и
        сегодня действительно пишет дневник — без всякого основания согласия."""
        response = self._client(person).post("/api/v1/nutrition/food-log/", self.WRITE, format="json")

        assert response.status_code == 201, response.content
        assert FoodLog.objects.filter(user=person).count() == 1

    @pytest.mark.xfail(strict=True, reason="DRF-2777: /nutrition/food-log/ пишет без основания food_diary_processing")
    def test_the_client_app_food_log_endpoint_refuses_a_write_without_the_consent(self, person):
        response = self._client(person).post("/api/v1/nutrition/food-log/", self.WRITE, format="json")

        assert response.status_code in (403, 422), response.content
        assert FoodLog.objects.filter(user=person).count() == 0
