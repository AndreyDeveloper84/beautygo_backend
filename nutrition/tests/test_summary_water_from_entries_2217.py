"""DRF-2217 — вода в сводке дня берётся из тех записей, которые люди делают.

Сводка дня (``NutritionSummaryService``) читала воду из ``WaterLog`` —
строк кнопочного мобильного трекера Slice 4, — а бот и Mini App пишут воду
через ``internal/water/`` в ``WaterEntry`` (DRF-302). В итоге «0 мл» в
сводке, в AI-комментарии к ней (он берёт тот же агрегат) и в отчёте бота —
при записанной воде.

Правила этого файла:

* **один источник** — ``WaterEntry``, тот же, что у экрана воды
  (``WaterEntryService.today``): сумма по записям, отменённые не считаются,
  итог не ниже нуля;
* **миллилитры из самих записей**, а не «стаканов × 250»: размер стакана
  настраиваемый (W-1, CD §67), и новый размер действует только для будущих
  записей — старые записи хранят свои мл;
* **сутки — те же, что у еды в этой сводке** (пояс человека, DRF-2099),
  иначе глоток в 00:30 по Москве уезжал бы во вчерашний UTC-день.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_tz
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from nutrition.models import NutritionProfile, WaterEntry, WaterLog
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from users.models import User

pytestmark = pytest.mark.django_db

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture
def person(db) -> User:
    user = User.objects.create(username="bot:2217", role="client", is_proxy=True)
    NutritionProfile.objects.create(user=user, timezone="Europe/Moscow")
    return user


def _today_msk() -> date:
    return timezone.now().astimezone(MSK).date()


def _at_msk(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)


def _entry(user: User, ml: int, ts: datetime, *, deleted: bool = False) -> WaterEntry:
    return WaterEntry.objects.create(
        user=user,
        ts=ts,
        ml=ml,
        water_ml=float(ml),
        deleted_at=timezone.now() if deleted else None,
    )


def _water(user: User, day: date) -> int:
    return NutritionSummaryService().summary(user_id=user.id, day=day).water_ml


class TestSummaryWaterComesFromEntries:
    def test_logged_250_ml_shows_250_ml(self, person) -> None:
        """Узел листа: записал 250 мл → в сводке 250 мл."""
        day = _today_msk()
        _entry(person, 250, _at_msk(day, 12))
        assert _water(person, day) == 250

    def test_millilitres_come_from_the_entries_not_glasses_times_250(self, person) -> None:
        """W-1: стакан сменили с 300 на 200 — старая запись хранит свои 300."""
        day = _today_msk()
        _entry(person, 300, _at_msk(day, 9))
        _entry(person, 200, _at_msk(day, 15))
        assert _water(person, day) == 500  # не 2 × 250

    def test_undone_entry_is_not_counted(self, person) -> None:
        day = _today_msk()
        _entry(person, 250, _at_msk(day, 10))
        _entry(person, 400, _at_msk(day, 11), deleted=True)
        assert _water(person, day) == 250

    def test_the_day_is_the_persons_day_like_the_food_in_this_summary(self, person) -> None:
        """00:30 по Москве — это сегодня, хотя по UTC ещё вчера (21:30)."""
        day = _today_msk()
        _entry(person, 250, _at_msk(day, 0, 30))
        _entry(person, 150, _at_msk(day, 23, 50))
        _entry(person, 999, _at_msk(day + timedelta(days=1), 0, 10))  # завтра — мимо
        assert _water(person, day) == 400

    def test_summary_equals_the_water_screen_for_the_same_day(self, person) -> None:
        """Один агрегат: сводка и экран воды (``WaterEntryService.today``) не спорят."""
        from nutrition.services.water_entry_service import WaterEntryService

        day = _today_msk()
        _entry(person, 250, _at_msk(day, 8))
        _entry(person, 330, _at_msk(day, 13))
        screen = WaterEntryService().today(person.id).today_total_water_ml
        assert screen == 580  # положительная пара: экран правда видит записи
        assert _water(person, day) == screen


class TestOneSourceNotTwo:
    def test_legacy_waterlog_rows_are_not_the_source(self, person) -> None:
        """Источник один — ``WaterEntry``. Строка старого трекера сводку не меняет.

        Положительная пара на тех же сутках: запись ``WaterEntry`` видна.
        """
        day = _today_msk()
        _entry(person, 250, _at_msk(day, 12))
        WaterLog.objects.create(user=person, amount_ml=500, logged_at=_at_msk(day, 12))
        assert _water(person, day) == 250


class TestTheCommentSeesTheSameWater:
    def test_ai_comment_facts_carry_the_entries_total(self, person) -> None:
        """AI-комментарий к сводке берёт тот же агрегат — «0 мл» было и там."""
        day = _today_msk()
        _entry(person, 250, _at_msk(day, 12))
        seen: list[int] = []

        def _capture(self, *, user_id, day, facts):  # noqa: ANN001
            seen.append(facts.water_ml)
            return None

        with patch(
            "nutrition.services.ai_comment_service.AICommentService.comment_for", _capture
        ):
            NutritionSummaryService().summary(user_id=person.id, day=day, with_comment=True)
        assert seen == [250]


def test_fixture_person_has_a_timezone(person) -> None:
    """Стража стражи: без пояса тест «сутки человека» проверял бы UTC."""
    assert NutritionProfile.objects.get(user=person).timezone == "Europe/Moscow"
    assert datetime.now(dt_tz.utc).tzinfo is not None
