"""Один ориентир воды для экрана и сводки; активные для напоминания — по WaterEntry (DRF-2257).

Находки DRF-2217 (#532, вне объёма):

1. Экран воды (``WaterEntryService.today`` → ``today_norm_water_ml``)
   отдавал ДЕЙСТВУЮЩИЙ ориентир — ``daily_water_ml`` под предикатом
   происхождения ``fluids_confirmed`` (§85 раздел 4: справочник по полу,
   подтверждённый человеком, — ``ayla_calculated``; или названный им сам —
   ``user_entered``). Сводка дня (``NutritionSummaryService.summary`` →
   ``water_goal_ml``) — всегда «нет ориентира». Человек видел разное в двух
   местах про одно и то же.
2. Напоминание «отстаёшь по воде» выбирало активных по ``WaterLog``
   (кнопочный трекер), а бот и Mini App пишут в ``WaterEntry``.

Правильный ориентир — тот, что у экрана: он и есть методика §85 п.4 с
подписью происхождения. Выводится однозначно; числа никто не выдумывает.

* t1 — ``ayla_calculated`` с водой → сводка и экран называют ОДНО число;
* t2 — ``user_entered`` вода → то же, одно число;
* t3 — предложение (``ayla_proposed``), ``unknown_legacy``, ``none`` → и
  сводка, и экран — отсутствие (ключа нет), не 0;
* t4 — один источник по построению: сводка и экран зовут одну функцию
  :func:`water_entry_service.fluid_target_ml` (подменили её — обе стороны
  называют подменённое число);
* r1 — напоминание: человек, пишущий воду только через ``WaterEntry``, —
  активный (попадает в ``skipped`` при выключенной рассылке); человек только
  с ``WaterLog`` — нет;
* r2 — удалённая (``deleted_at``) запись активности не делает; запись
  старше окна — тоже.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from nutrition.models import NutritionProfile, WaterEntry, WaterLog
from nutrition.services import water_entry_service
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from nutrition.services.water_entry_service import WaterEntryService
from users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def person():
    return User.objects.create_user(username="drf2257", password="x", role="client")


def _profile(user, *, source: str, water: int | None) -> NutritionProfile:
    profile, _ = NutritionProfile.objects.get_or_create(user=user)
    profile.targets_source = source
    profile.fluids_source = source
    profile.daily_water_ml = water
    profile.save()
    return profile


def _both(user) -> tuple[int | None, int | None]:
    today = datetime.now(UTC).date()
    summary = NutritionSummaryService().summary(user_id=user.id, day=today)
    screen = WaterEntryService().today(user.id)
    return summary.water_goal_ml, screen.today_norm_water_ml


class TestT1CalculatedIsOneNumber:
    def test_summary_and_screen_agree(self, person) -> None:
        _profile(person, source="ayla_calculated", water=2200)
        assert _both(person) == (2200, 2200)


class TestT2UserEnteredIsOneNumber:
    def test_summary_and_screen_agree(self, person) -> None:
        _profile(person, source="user_entered", water=1800)
        assert _both(person) == (1800, 1800)


class TestT3NotActingIsAbsentEverywhere:
    @pytest.mark.parametrize("source", ["ayla_proposed", "unknown_legacy", "none"])
    def test_absent_in_both(self, person, source: str) -> None:
        _profile(person, source=source, water=2100)
        assert _both(person) == (None, None)

    def test_no_profile_is_absent_in_both(self, person) -> None:
        assert _both(person) == (None, None)


class TestT4OneSourceByConstruction:
    def test_both_sides_call_the_same_function(self, person) -> None:
        _profile(person, source="ayla_calculated", water=2200)
        with patch.object(water_entry_service, "fluid_target_ml", return_value=1234):
            assert _both(person) == (1234, 1234)


class TestR1ReminderCountsWaterEntry:
    def test_a_water_entry_writer_is_active(self, person) -> None:
        from notifications.tasks import dispatch_water_reminders

        now = datetime.now(UTC)
        WaterEntry.objects.create(user=person, ml=250, water_ml=250.0, ts=now - timedelta(hours=1))
        assert dispatch_water_reminders() == {"queued": 0, "skipped": 1}

    def test_a_water_log_only_writer_is_not(self, person) -> None:
        from notifications.tasks import dispatch_water_reminders

        now = datetime.now(UTC)
        WaterLog.objects.create(user=person, amount_ml=250, logged_at=now - timedelta(hours=1))
        # Присутствие: запись есть, и она в окне.
        assert WaterLog.objects.filter(user=person).exists()
        assert dispatch_water_reminders() == {"queued": 0, "skipped": 0}


class TestR2DeletedAndStaleAreNotActive:
    def test_deleted_and_old_entries_do_not_count(self, person, settings) -> None:
        from notifications.tasks import dispatch_water_reminders

        now = datetime.now(UTC)
        WaterEntry.objects.create(user=person, ml=250, water_ml=250.0, ts=now - timedelta(hours=1), deleted_at=now)
        old = User.objects.create_user(username="drf2257_stale", password="x", role="client")
        WaterEntry.objects.create(
            user=old,
            ml=250,
            water_ml=250.0,
            ts=now - timedelta(days=settings.WATER_REMINDER_ACTIVE_WINDOW_DAYS + 2),
        )
        fresh = User.objects.create_user(username="drf2257_fresh", password="x", role="client")
        WaterEntry.objects.create(user=fresh, ml=250, water_ml=250.0, ts=now - timedelta(hours=2))
        assert dispatch_water_reminders() == {"queued": 0, "skipped": 1}
