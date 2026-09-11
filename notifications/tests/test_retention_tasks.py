"""Tests for water + beauty-insight retention beat tasks (Slice N4).

Both tasks are idempotent — re-running on the same window finds
already-sent Notification rows and skips. Tests prove that.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest

from notifications.models import Notification
from notifications.tasks import (
    dispatch_beauty_insights,
    dispatch_water_reminders,
)
from nutrition.models import FoodLog, NutritionProfile, WaterLog
from users.models import User


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="ret-client", password="x", role="client",
        phone="+79997770000",
    )


@pytest.fixture
def other_active_client(db):
    return User.objects.create_user(
        username="ret-other", password="x", role="client",
        phone="+79997770001",
    )


# Tests run with deliver_notification.delay patched so we don't need a
# Celery worker; assertions look at persisted Notification rows.
@pytest.fixture(autouse=True)
def _no_celery_dispatch():
    with patch("notifications.tasks.deliver_notification.delay"):
        yield


def _now_utc() -> datetime:
    return datetime.now(dt_tz.utc)


def _today_start() -> datetime:
    return datetime.combine(
        _now_utc().date(), datetime.min.time(), tzinfo=dt_tz.utc,
    )


def _own_norm(user, ml: int = 2000):
    """Дать человеку СВОЮ дневную норму воды — из его анкеты питания.

    Раньше эту роль играла ``NUTRITION_DEFAULT_WATER_GOAL_ML``, и норма
    в тестах бралась ниоткуда ровно так же, как в бою. Тесты ниже про
    механику рассылки — про дедуп, про окно активности, про то, что
    чужая вода не считается за свою, — и норма им нужна лишь как
    предусловие: без неё напоминания не бывает вовсе (см.
    ``TestWaterReminderDoesNotInventANorm``).
    """
    return NutritionProfile.objects.create(user=user, daily_water_ml=ml)


# ---------------------------------------------------------------------------
# Water reminders
# ---------------------------------------------------------------------------


class TestDispatchWaterReminders:
    def test_no_active_users_returns_zero(self, db):
        assert dispatch_water_reminders() == {"queued": 0, "skipped": 0}

    def test_nobody_is_reminded_while_there_is_no_fluid_target(
        self, client_user,
    ):
        """Отстаёт человек или нет — решать НЕ ПО ЧЕМУ, и пуш не уходит.

        Тест назывался ``test_user_behind_goal_gets_reminder`` и был
        главным подтверждением, что механика работает. «Отстаёт»
        определялось сравнением с ``NutritionProfile.daily_water_ml`` —
        выходом формулы ``30 мл × вес`` (+300/+700), которую владелец
        снял 09.09.2026 (§82). Пока в столбце результат снятой формулы,
        прочитать его значит эту формулу применить.

        Следствие названо прямо, а не спрятано: напоминание про воду не
        уходит НИКОМУ до утверждения методики (§85, раздел 4). Это самое
        острое из мест, где выдумка уезжала человеку САМА, дважды в
        день, и читается она с заблокированного экрана. Выдуманный повод
        написать человеку хуже молчания.

        Молчание при этом СЧИТАЕТСЯ: ``skipped`` растёт, и в логе видно,
        скольким сегодня не написали. Молчаливая пустота была бы отказом
        без имени.
        """
        _own_norm(client_user)
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        assert result["queued"] == 0
        assert result["skipped"] == 1
        assert not Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).exists()

    def test_repeat_beats_stay_silent_too(self, client_user):
        """Дедуп проверять стало не на чем — проверяется молчание обоих.

        Тест назывался ``test_idempotent_within_today`` и сторожил, что
        два удара планировщика дают одно сообщение. Сообщений теперь
        ноль, и утверждение перевёрнуто: ни один удар не пишет человеку.
        Дедуп вернётся вместе с ориентиром — код рассылки цел.
        """
        _own_norm(client_user)
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        dispatch_water_reminders()
        result = dispatch_water_reminders()
        assert result["queued"] == 0
        assert Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).count() == 0

    def test_dormant_users_excluded(self, db, client_user):
        # WaterLog 30 days ago — outside the 7-day active window.
        old = _now_utc() - timedelta(days=30)
        WaterLog.objects.create(user=client_user, amount_ml=250, logged_at=old)
        result = dispatch_water_reminders()
        assert result == {"queued": 0, "skipped": 0}

    def test_other_users_water_only_counts_for_themselves(
        self, client_user, other_active_client,
    ):
        _own_norm(client_user)
        _own_norm(other_active_client)
        # client_user is at 250 (behind), other user is past goal.
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        WaterLog.objects.create(
            user=other_active_client, amount_ml=2200, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        # Оба активны, обоим не пишут: ориентира нет ни у кого. Тест
        # сторожил, что чужая вода не считается за свою; проверять это
        # стало не на чем, но ОКНО АКТИВНОСТИ он держит по-прежнему —
        # оба человека попали в счётчик, а не выпали из выборки.
        assert result["queued"] == 0
        assert result["skipped"] == 2


# ---------------------------------------------------------------------------
# Beauty insights
# ---------------------------------------------------------------------------


class TestDispatchBeautyInsights:
    def test_no_active_users_returns_zero(self, db):
        assert dispatch_beauty_insights()["queued"] == 0

    def test_active_user_gets_insight(self, client_user):
        # Active = at least one FoodLog in the last 7 days.
        FoodLog.objects.create(
            user=client_user, dish_name="борщ",
            calories=147, protein_g=4.8, fat_g=6.6, carbs_g=20.1,
            meal_type="lunch", logged_at=_now_utc(),
        )
        result = dispatch_beauty_insights()
        assert result["queued"] == 1
        n = Notification.objects.get(
            user=client_user, template_id="beauty_insight",
        )
        assert "1 приёмов пищи" in n.body or "приём" in n.body

    def test_idempotent_within_week(self, client_user):
        FoodLog.objects.create(
            user=client_user, dish_name="x",
            calories=100, protein_g=1, fat_g=1, carbs_g=10,
            meal_type="lunch", logged_at=_now_utc(),
        )
        dispatch_beauty_insights()
        result = dispatch_beauty_insights()
        assert result["queued"] == 0
        assert result["skipped"] == 1

    def test_user_cap_respected(self, db, settings):
        # 5 active users, cap=2 → only 2 receive insights this tick.
        settings.BEAUTY_INSIGHT_USER_CAP = 2
        users = []
        for i in range(5):
            u = User.objects.create_user(
                username=f"cap-{i}", password="x", role="client",
                phone=f"+7999777{1000+i:04d}",
            )
            FoodLog.objects.create(
                user=u, dish_name="x",
                calories=100, protein_g=1, fat_g=1, carbs_g=10,
                meal_type="lunch", logged_at=_now_utc(),
            )
            users.append(u)
        result = dispatch_beauty_insights()
        assert result["queued"] == 2

    def test_dormant_user_excluded(self, client_user):
        # FoodLog 14 days ago — outside the 7-day active window.
        old = _now_utc() - timedelta(days=14)
        FoodLog.objects.create(
            user=client_user, dish_name="x",
            calories=100, protein_g=1, fat_g=1, carbs_g=10,
            meal_type="lunch", logged_at=old,
        )
        result = dispatch_beauty_insights()
        assert result["queued"] == 0

    def test_failed_insight_build_doesnt_break_cohort(
        self, client_user, other_active_client,
    ):
        # First user's _build_insight_text raises; second still gets a
        # notification so one bad LLM call doesn't drop the whole tick.
        for u in (client_user, other_active_client):
            FoodLog.objects.create(
                user=u, dish_name="x",
                calories=100, protein_g=1, fat_g=1, carbs_g=10,
                meal_type="lunch", logged_at=_now_utc(),
            )

        original = "notifications.tasks._build_insight_text"
        side_effects = iter([RuntimeError("LLM down"), "ok-text"])

        def flaky(_user):
            v = next(side_effects)
            if isinstance(v, Exception):
                raise v
            return v

        with patch(original, flaky):
            result = dispatch_beauty_insights()

        assert result["queued"] == 1
        assert result["failed"] == 1


# ---------------------------------------------------------------------------
# Напоминание про воду не придумывает норму
# ---------------------------------------------------------------------------


class TestWaterReminderDoesNotInventANorm:
    """Самое острое из трёх мест: сообщение уходит человеку САМО.

    Экран человек открывает сам, а это — исходящий пуш: выдуманная
    норма приезжала на телефон без всякого запроса, дважды в день, и
    называла себя «твоей». Дорога была двухступенчатой:

    1. Знаменатель брался из ``NUTRITION_DEFAULT_WATER_GOAL_ML``
       (2000 мл = ровно восемь стаканов по 250) и решал ДВЕ вещи: кого
       признать отстающим и какое число написать в тексте. Число из
       текста убрали, источник сменили на анкету.
    2. В анкете лежал результат формулы ``30 мл × вес`` (+300/+700) —
       то же чужое число с лишним шагом. Владелец снял и формулу (§82),
       поэтому снят последний читатель: решать «отстаёт» не по чему.

    Пуш не уходит никому до утверждения методики (§85, раздел 4). Не
    уходит и тем, у кого столбец ещё заполнен старым значением —
    миграцию данных не делали, а «новым не считаем, старым считаем»
    было бы половинчатой правкой.
    """

    def test_no_anketa_means_no_reminder(self, client_user):
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        result = dispatch_water_reminders()
        assert result["queued"] == 0
        assert not Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).exists()

    def test_a_stale_column_value_does_not_bring_the_push_back(
        self, client_user,
    ):
        """Заполненный столбец у существующего клиента ничего не решает.

        Самое невыгодное предусловие для правки: миграцию данных не
        делали (отдельный срез), и у людей с пройденной анкетой в
        ``daily_water_ml`` до сих пор лежит ``30 мл × вес``. Пуш не
        должен уходить и им — иначе снятие формулы означало бы «новым
        не считаем, старым считаем».
        """
        NutritionProfile.objects.create(user=client_user, daily_water_ml=1500)
        WaterLog.objects.create(
            user=client_user, amount_ml=250, logged_at=_now_utc(),
        )
        assert dispatch_water_reminders()["queued"] == 0
        assert not Notification.objects.filter(
            user=client_user, template_id="water_reminder",
        ).exists()
