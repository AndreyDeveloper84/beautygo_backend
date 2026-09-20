"""План-B — питание в плане (DRF-2124): связать три понятия цели, не слить (В-4).

``ClientGoal.goal_key`` (цель человека), ``NutritionProfile.goal``
lose/maintain/gain (параметр расчёта) и service goal остаются разными. Здесь
ровно две связи, обе — данные и факты, не решения:

1. **Подсказка** ``PlanTemplate.nutrition_goal_hint`` — какой ``goal`` анкеты
   питания обычно выбирают под курируемую цель (§51): ``body_shape`` →
   lose или maintain (выбор человека), ``recharge`` / ``self_care`` →
   maintain, остальные — без подсказки. Читатель — анкета питания в боте на
   шаге «Какая у тебя цель?»: подсветить, НЕ предвыбрать и не пропустить шаг
   (§7.1/§5.1). Наружу — в ``decision-context`` рядом с активной целью, к
   которой относится: ``known.goal.nutrition_goal_hint``; ``null`` — нет
   подсказки (§103: нет входа — ``None``, не пустышка).
2. **Факт** ``within_target_count`` у действия ``log_food`` в ``plan_lite`` —
   дни ведра, когда ориентир по калориям подтверждён (``calories_confirmed``,
   F1(б)) И дневная сумма ≤ ориентира. Без подтверждённого ориентира —
   ``null``, не 0 (§103). Никаких процентов (В-5, §85).

Граница В-1 держится: ``plan_lite*.py`` профиль питания не читает — счёт
дней в ориентире живёт поставщиком в ``nutrition`` (направление В-2: wellness
читает nutrition, не наоборот).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from goals.models import ClientGoal
from nutrition.models import FoodLog, NutritionProfile
from users.models import User
from wellness.models import PlanTemplate

VALID_TOKEN = "test-ayla-internal-token-nutrition-in-plan"
PLAN_URL = "/api/v1/internal/me/plan-lite/"
CTX_URL = "/api/v1/internal/me/wellness-context/"
DECISION_URL = "/api/v1/internal/me/decision-context/"
OWNER = "bot:nutrition-in-plan-owner"

#: Таблица подсказки §51 — вторая копия, сверяется с сидом.
HINTS_BY_GOAL_KEY: dict[str, list[str]] = {
    "body_shape": ["lose", "maintain"],
    "event": [],
    "new_look": [],
    "recharge": ["maintain"],
    "relax": [],
    "self_care": ["maintain"],
    "skin_care": [],
}

FULL_INPUTS = {
    "gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0,
    "activity_coefficient": 1.375, "goal": "lose",
}


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_LITE_ENABLED = True


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(
        username=OWNER, password="x", role="client", phone="+79995002124", is_proxy=True,
    )


@pytest.fixture
def seeded(db) -> str:
    out = StringIO()
    call_command("seed_plan_templates", stdout=out)
    return out.getvalue()


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _goal(owner: User, key: str) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key=key, source_channel="miniapp")


def _at(day: date, hour: int = 10) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour=hour)))


def _log(owner: User, day: date, kcal: float, hour: int = 10) -> FoodLog:
    return FoodLog.objects.create(
        user=owner, dish_name="еда", calories=kcal, logged_at=_at(day, hour),
    )


def _monday_of_this_week() -> date:
    today = timezone.localdate()
    return today - timedelta(days=today.weekday())


def _plan_lite(client: APIClient) -> dict:
    resp = client.get(CTX_URL)
    assert resp.status_code == 200, resp.content[:400]
    return resp.json()["data"]["plan_lite"]


def _food_action(plan_lite: dict) -> dict:
    [action] = [a for a in plan_lite["actions"] if a["action_type"] == "log_food"]
    return action


# ─── 1. подсказка — данные шаблона ───────────────────────────────────────────


class TestHintIsTemplateData:
    def test_the_seed_carries_the_hint_table_verbatim(self, seeded) -> None:
        active = {t.goal_key: t for t in PlanTemplate.objects.filter(is_active=True)}
        assert set(active) == set(HINTS_BY_GOAL_KEY)  # присутствие раньше сверки
        for key, hint in HINTS_BY_GOAL_KEY.items():
            assert active[key].nutrition_goal_hint == hint, key

    def test_body_shape_hints_both_and_decides_nothing(self, seeded) -> None:
        """«lose или maintain» — две подсказки, не один предвыбор: человек выбирает."""
        hint = PlanTemplate.objects.get(goal_key="body_shape", is_active=True).nutrition_goal_hint
        assert hint == ["lose", "maintain"]
        assert len(hint) == 2

    def test_a_changed_hint_is_a_new_template_version(self, seeded) -> None:
        """Подсказка — часть строки таблицы: её правка версионируется как текст."""
        from wellness import plan_lite_templates

        rows = [dict(r) for r in plan_lite_templates.PLAN_TEMPLATES_SEED]
        target = next(r for r in rows if r["goal_key"] == "relax")
        target["nutrition_goal_hint"] = ["maintain"]

        report = plan_lite_templates.seed_plan_templates(rows)

        assert report.created == 1 and report.deactivated == 1
        versions = list(
            PlanTemplate.objects.filter(goal_key="relax").order_by("version")
            .values_list("version", "is_active", "nutrition_goal_hint")
        )
        assert versions == [(1, False, []), (2, True, ["maintain"])]

    def test_the_seed_is_idempotent_with_hints(self, seeded) -> None:
        from wellness import plan_lite_templates

        report = plan_lite_templates.seed_plan_templates()
        assert report.created == 0 and report.deactivated == 0
        assert report.unchanged == len(HINTS_BY_GOAL_KEY)

    @pytest.mark.parametrize("bad", [["slim"], ["lose", "lose"], "lose"], ids=["опечатка", "дубль", "не-список"])
    def test_hint_values_are_anketa_goals_only(self, db, bad) -> None:
        """Подсказка говорит на языке анкеты питания (lose/maintain/gain) — и только.
        Остальные поля валидны, чтобы ошибка была именно про подсказку."""
        from django.core.exceptions import ValidationError

        t = PlanTemplate(
            goal_key="x", actions=[{"action_type": "log_water", "cadence": "per_day", "target_count": 1}],
            why_text="w", nutrition_goal_hint=bad,
        )
        with pytest.raises(ValidationError) as ei:
            t.full_clean()
        assert set(ei.value.message_dict) == {"nutrition_goal_hint"}

    def test_a_valid_hint_passes_the_same_clean(self, db) -> None:
        """Положительная стража валидатора: правильная подсказка не падает."""
        t = PlanTemplate(
            goal_key="x", actions=[{"action_type": "log_water", "cadence": "per_day", "target_count": 1}],
            why_text="w", nutrition_goal_hint=["lose", "maintain"],
        )
        t.full_clean()

    def test_a_seed_row_with_a_typo_fails_before_touching_the_table(self, seeded) -> None:
        """Сид с опечаткой падает целиком: прежние активные версии на месте."""
        from django.core.exceptions import ValidationError

        from wellness import plan_lite_templates

        rows = [dict(r) for r in plan_lite_templates.PLAN_TEMPLATES_SEED]
        next(r for r in rows if r["goal_key"] == "relax")["nutrition_goal_hint"] = ["slim"]
        before = dict(PlanTemplate.objects.filter(is_active=True).values_list("goal_key", "version"))

        with pytest.raises(ValidationError):
            plan_lite_templates.seed_plan_templates(rows)

        after = dict(PlanTemplate.objects.filter(is_active=True).values_list("goal_key", "version"))
        assert after == before and len(after) == len(HINTS_BY_GOAL_KEY)


# ─── 2. подсказка — рядом с активной целью в decision-context ────────────────


class TestHintTravelsWithTheActiveGoal:
    def test_active_goal_with_a_hint_carries_it(self, seeded, owner) -> None:
        _goal(owner, "body_shape")

        doc = _api().get(DECISION_URL).json()["data"]

        assert doc["known"]["goal"]["goal_key"] == "body_shape"  # присутствие
        assert doc["known"]["goal"]["nutrition_goal_hint"] == ["lose", "maintain"]

    @pytest.mark.parametrize("key", ["relax", "event"])
    def test_active_goal_without_a_hint_says_null(self, seeded, owner, key) -> None:
        """§103 — нет подсказки — ``null``, не пустой список и не пропуск ключа."""
        _goal(owner, key)

        goal = _api().get(DECISION_URL).json()["data"]["known"]["goal"]

        assert goal["goal_key"] == key
        assert "nutrition_goal_hint" in goal
        assert goal["nutrition_goal_hint"] is None

    def test_no_template_means_null_too(self, owner) -> None:
        """Сид не прогнан — таблицы нет — подсказки нет, а не 500."""
        _goal(owner, "body_shape")
        assert not PlanTemplate.objects.exists()

        goal = _api().get(DECISION_URL).json()["data"]["known"]["goal"]

        assert goal["goal_key"] == "body_shape"
        assert goal["nutrition_goal_hint"] is None

    def test_the_hint_is_a_hint_not_the_anketa_goal(self, seeded, owner) -> None:
        """Связать, не слить (В-4): подсказка не пишет ``NutritionProfile.goal``."""
        _goal(owner, "body_shape")
        _api().get(DECISION_URL)
        assert not NutritionProfile.objects.filter(user=owner).exists()


# ─── 3. within_target_count — факт по подтверждённому ориентиру ──────────────


def _confirmed_profile(owner: User, kcal: int) -> NutritionProfile:
    return NutritionProfile.objects.create(
        user=owner,
        targets_source=NutritionProfile.TargetsSource.AYLA_CALCULATED,
        calories_source=NutritionProfile.TargetsSource.AYLA_CALCULATED,
        daily_kcal=kcal,
        **FULL_INPUTS,
    )


def _proposed_profile(owner: User, kcal: int) -> NutritionProfile:
    return NutritionProfile.objects.create(
        user=owner,
        targets_source=NutritionProfile.TargetsSource.AYLA_PROPOSED,
        calories_source=NutritionProfile.TargetsSource.AYLA_PROPOSED,
        daily_kcal=kcal,
        **FULL_INPUTS,
    )


FOOD_5_PER_WEEK = {"action_type": "log_food", "cadence": "per_week", "target_count": 5}
WATER_6_PER_DAY = {"action_type": "log_water", "cadence": "per_day", "target_count": 6}


class TestWithinTargetCount:
    @pytest.fixture
    def plan(self, seeded, owner):
        _goal(owner, "body_shape")
        resp = _api().post(
            PLAN_URL, {"actions": [FOOD_5_PER_WEEK, WATER_6_PER_DAY], "template_version": 1},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]

    def test_without_a_confirmed_target_it_is_null_not_zero(self, plan, owner) -> None:
        monday = _monday_of_this_week()
        _log(owner, monday, 400)
        _log(owner, monday + timedelta(days=1), 500)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 2  # присутствие: дни с записью посчитаны
        assert "within_target_count" in food
        assert food["within_target_count"] is None

    def test_a_proposed_but_unconfirmed_target_is_still_null(self, plan, owner) -> None:
        """§5.1 — предложение не ориентир: «в ориентире» не считается от неподтверждённого числа."""
        _proposed_profile(owner, 1600)
        monday = _monday_of_this_week()
        _log(owner, monday, 400)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 1
        assert food["within_target_count"] is None

    def test_days_within_target_are_counted_by_daily_sum(self, plan, owner) -> None:
        _confirmed_profile(owner, 1600)
        monday = _monday_of_this_week()
        # день 1 — два приёма, сумма 1500 ≤ 1600 → в ориентире
        _log(owner, monday, 700, hour=9)
        _log(owner, monday, 800, hour=19)
        # день 2 — два приёма, сумма 1700 > 1600 → записан, но не в ориентире
        _log(owner, monday + timedelta(days=1), 900, hour=9)
        _log(owner, monday + timedelta(days=1), 800, hour=19)
        # день 3 — ровно ориентир → в ориентире (≤)
        _log(owner, monday + timedelta(days=2), 1600)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 3
        assert food["within_target_count"] == 2

    def test_a_day_without_entries_is_not_within_target(self, plan, owner) -> None:
        """Пустой день — не «0 ≤ ориентира»: в ориентире только дни с записью."""
        _confirmed_profile(owner, 1600)
        monday = _monday_of_this_week()
        _log(owner, monday, 1000)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 1
        assert food["within_target_count"] == 1

    def test_within_never_exceeds_done(self, plan, owner) -> None:
        _confirmed_profile(owner, 5000)
        monday = _monday_of_this_week()
        for i in range(7):
            _log(owner, monday + timedelta(days=i), 300)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 5  # не больше target_count
        assert food["within_target_count"] <= food["done_count"]

    def test_per_day_food_counts_the_day_not_the_entries(self, seeded, owner) -> None:
        """Единица — день: при ``per_day`` три записи в ориентире — «сегодня в ориентире» (1), не 3."""
        _goal(owner, "body_shape")
        resp = _api().post(
            PLAN_URL,
            {"actions": [{"action_type": "log_food", "cadence": "per_day", "target_count": 3}],
             "template_version": 1},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:400]
        _confirmed_profile(owner, 1600)
        today = timezone.localdate()
        for hour in (9, 13, 19):
            _log(owner, today, 300, hour=hour)

        food = _food_action(_plan_lite(_api()))

        assert food["done_count"] == 3  # записи
        assert food["within_target_count"] == 1  # день

    def test_water_and_booking_carry_no_within_target(self, plan, owner) -> None:
        """Факт про калории — только у ``log_food``; у воды и записи ключа нет."""
        _confirmed_profile(owner, 1600)
        plan_lite = _plan_lite(_api())
        for action in plan_lite["actions"]:
            if action["action_type"] != "log_food":
                assert "within_target_count" not in action, action

    def test_no_percent_no_ratio(self, plan, owner) -> None:
        """В-5 / §85: два целых числа, ничего производного."""
        _confirmed_profile(owner, 1600)
        _log(owner, _monday_of_this_week(), 1000)

        food = _food_action(_plan_lite(_api()))

        assert set(food) == {
            "action_type", "cadence", "target_count", "done_count", "bucket",
            "within_target_count",
        }
        assert isinstance(food["within_target_count"], int)


# ─── 4. граница В-1: plan_lite не читает профиль ─────────────────────────────


class TestPlanLiteStillDoesNotTouchTheProfile:
    def test_plan_lite_modules_do_not_import_nutrition_profile(self) -> None:
        from wellness.tests.test_plan_lite_2101 import (
            FORBIDDEN_IMPORT_NAMES,
            _imported_names,
            _plan_lite_modules,
        )

        modules = _plan_lite_modules()
        assert modules
        for module in modules:
            names = _imported_names(module.read_text(encoding="utf-8"))
            assert not (FORBIDDEN_IMPORT_NAMES & names), module.name
            assert "daily_kcal" not in names, module.name
