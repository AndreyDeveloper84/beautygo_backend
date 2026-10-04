"""DRF-2766 фаза 2 — итог дня включает оценки ИИ и говорит об этом.

Решение владельца 04.10, п.3 (пересмотр iv DRF-2761): человек записал еду —
итог её учитывает. Условия владельца, каждое — узел:

* у записи её число: проверенное, а если его нет, оценка ИИ; проверенное и
  оценка одной порции НЕ складываются;
* итог называет, сколько записей вошло оценкой (``calories_ai_included``) —
  поверхность покажет «≈ …, включая оценки ИИ»;
* запись без какого-либо значения калорий — не ноль; итог неполный
  (``calories_unscored``);
* то же правило у дней недели (``diary/days``) — день и неделя не
  расходятся;
* выводы остаются на проверенном: комментарий дня при записях без БЖУ не
  запрашивается, как и прежде.

Данные синтетические.
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_tz
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from nutrition.models import FoodLog
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-2766"
OWNER = "bot:2766"
DAYS_URL = "/api/v1/nutrition/internal/diary/days/"
SUMMARY_URL = "/api/v1/nutrition/internal/summary/"
DAY = date(2026, 9, 16)
NOON = datetime(2026, 9, 16, 12, 0, tzinfo=dt_tz.utc)


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def owner(db) -> User:
    return User.objects.create(username=OWNER, role="client", is_proxy=True)


def _client() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_SERVICE_TOKEN"] = SERVICE_TOKEN
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = OWNER
    return c


def _entry(user, *, kcal=None, ai=None, macros=None, dish="блюдо", hour=12) -> FoodLog:
    macros = macros if macros is not None else (None, None, None)
    return FoodLog.objects.create(
        user=user,
        dish_name=dish,
        portion_multiplier=1.0,
        calories=kcal,
        ai_calories=ai,
        protein_g=macros[0],
        fat_g=macros[1],
        carbs_g=macros[2],
        meal_type="lunch",
        entry_origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED,
        logged_at=NOON.replace(hour=hour),
    )


def _totals(user):
    return NutritionSummaryService().summary(user_id=user.id, day=DAY).totals


class TestTheDayTotal:
    def test_the_owners_borscht_case_an_estimate_alone_is_the_total(self, owner):
        """Записал «≈ 150 ккал · Оценка ИИ», а итог был «0 из 2588» — теперь 150."""
        _entry(owner, ai=150.0, dish="запиши в дневник борщ")

        totals = _totals(owner)

        assert totals.calories == 150.0
        assert totals.calories_ai_included == 1
        assert totals.calories_unscored == 0

    def test_verified_and_estimated_entries_add_up(self, owner):
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        _entry(owner, ai=300.0, dish="шакшука", hour=13)

        totals = _totals(owner)

        assert totals.calories == 447.0
        assert totals.calories_ai_included == 1
        assert totals.calories_unscored == 0

    def test_one_portion_is_never_counted_twice(self, owner):
        """На записи и проверенное, и оценка — берётся проверенное, одно."""
        _entry(owner, kcal=147.0, ai=999.0, macros=(4.8, 6.6, 20.1), dish="борщ")

        totals = _totals(owner)

        assert totals.calories == 147.0
        assert totals.calories_ai_included == 0

    def test_an_entry_without_any_value_is_not_zero_and_marks_the_total_incomplete(self, owner):
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        _entry(owner, dish="зыбзик", hour=13)

        totals = _totals(owner)

        assert totals.calories == 147.0
        assert totals.calories_unscored == 1
        assert totals.calories_ai_included == 0

    def test_a_day_of_only_unvalued_entries_has_no_total_not_zero(self, owner):
        _entry(owner, dish="зыбзик")

        totals = _totals(owner)

        assert totals.calories_unscored == 1
        assert totals.calories is None

    def test_an_empty_day_is_an_honest_zero(self, owner):
        totals = _totals(owner)

        assert totals.calories == 0.0
        assert (totals.calories_ai_included, totals.calories_unscored) == (0, 0)

    def test_macros_stay_verified_only_an_estimate_has_none(self, owner):
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        _entry(owner, ai=300.0, dish="шакшука", hour=13)

        totals = _totals(owner)

        assert totals.protein_g == 4.8
        assert totals.unscored_entries == 1


class TestTheWire:
    def test_the_summary_answer_carries_both_counters(self, owner):
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        _entry(owner, ai=300.0, dish="шакшука", hour=11)
        _entry(owner, dish="зыбзик", hour=13)

        resp = _client().get(SUMMARY_URL, {"date": DAY.isoformat()})

        assert resp.status_code == 200, resp.content[:300]
        data = resp.json()["data"]
        assert data["calories_total"] == 447.0
        assert data["calories_ai_included"] == 1
        assert data["calories_unscored"] == 1

    def test_the_week_uses_the_same_rule(self, owner):
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        _entry(owner, ai=300.0, dish="шакшука", hour=11)
        _entry(owner, dish="зыбзик", hour=13)

        resp = _client().get(DAYS_URL, {"from": DAY.isoformat(), "to": DAY.isoformat()})

        assert resp.status_code == 200, resp.content[:300]
        (row,) = resp.json()["data"]["days"]
        assert row["kcal"] == 447.0
        assert row["kcal_ai_included"] == 1
        assert row["uncounted_meals"] == 1
        assert row["meals_count"] == 3


class TestTheWeekNeverCountsAPortionTwice:
    def test_a_week_row_with_both_values_counts_the_verified_one(self, owner):
        _entry(owner, kcal=147.0, ai=999.0, macros=(4.8, 6.6, 20.1), dish="борщ")

        resp = _client().get(DAYS_URL, {"from": DAY.isoformat(), "to": DAY.isoformat()})

        (row,) = resp.json()["data"]["days"]
        assert row["meals_count"] == 1
        assert row["kcal"] == 147.0
        assert row["kcal_ai_included"] == 0


class TestConclusionsStayVerified:
    def test_the_day_comment_is_still_not_asked_when_an_entry_has_no_macros(self, owner):
        """Комментарий дня — вывод: при записи без БЖУ (оценка) не запрашивается."""
        _entry(owner, kcal=147.0, macros=(4.8, 6.6, 20.1), dish="борщ", hour=9)
        with patch(
            "nutrition.services.ai_comment_service.AICommentService.comment_for",
            return_value="комментарий",
        ) as asked:
            complete = NutritionSummaryService().summary(
                user_id=owner.id, day=DAY, with_comment=True
            )
        _entry(owner, ai=300.0, dish="шакшука", hour=13)
        with patch(
            "nutrition.services.ai_comment_service.AICommentService.comment_for",
            return_value="комментарий",
        ) as asked_again:
            with_estimate = NutritionSummaryService().summary(
                user_id=owner.id, day=DAY, with_comment=True
            )

        assert asked.call_count == 1
        assert complete.ai_comment == "комментарий"
        assert asked_again.call_count == 0
        assert with_estimate.ai_comment is None
