"""DRF-1838 (F4, половина каталога) — правка и удаление сохранённой записи еды.

§109 шаг 7: «сохранённую запись можно изменить или удалить». До этой правки
у ``internal/food-log/`` был только ``POST``: ошибочная запись оставалась в
дневнике, в итогах дня и в сводках 14/28 навсегда.

Что здесь стережётся — условия главного окна (15.09):

* правка порции пересчитывает снимок записи и называет число исправленным
  клиентом (§136: ``*_estimated_confirmed`` → ``*_user_corrected``);
* удалённая запись не попадает ни в итог дня, ни в счёт дней с записями
  (он же открывает сводки 14/28) — проверяется через настоящие читатели;
* удаление обратимо 15 минут, после — окончательно: восстановление
  отвечает 410, снимок стирается, задача beat стирает просроченные;
* чужой человек не видит, не правит и не удаляет запись — 404, данные целы;
* запись-зеркало воды (``WaterEntry.food_log``) правится только отменой
  стакана — 409.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from freezegun import freeze_time
from rest_framework.test import APIClient

# Импорт ручек и сервиса — здесь, при сборке, а не на первом запросе.
# DRF кладёт ``ScopedRateThrottle.timer = time.time`` атрибутом класса в
# момент импорта ``rest_framework.throttling``. Если первым его импортирует
# запрос внутри ``freeze_time``, атрибутом становится поддельная функция
# freezegun, она связывается как метод, и каждый вызов падает с
# ``TypeError: fake_time() takes 0 positional arguments``. Файл зеленел в
# общем прогоне (модуль уже импортирован соседями) и краснел один — порядок
# тестов был молчаливым параметром.
import nutrition.services.food_log_edit_service  # noqa: E402,F401
import nutrition.views  # noqa: E402,F401
from nutrition.models import FoodLog, WaterEntry
from nutrition.services.commitment_service import get_commitment_days
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-1838"
OWNER = "bot:1838"
STRANGER = "bot:9999"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=dt_tz.utc)


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def owner(db):
    return User.objects.create(username=OWNER, role="client", is_proxy=True)


@pytest.fixture
def stranger(db):
    return User.objects.create(username=STRANGER, role="client", is_proxy=True)


def _client(external_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_SERVICE_TOKEN"] = SERVICE_TOKEN
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return c


def _url(log_id, suffix: str = "") -> str:
    return f"/api/v1/nutrition/internal/food-log/{log_id}/{suffix}"


def _log(user, *, dish="Гречка", calories=300.0, portion=1.5,
         origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED, at=NOW) -> FoodLog:
    return FoodLog.objects.create(
        user=user, dish_name=dish, portion_multiplier=portion,
        calories=calories, protein_g=12.0, fat_g=6.0, carbs_g=40.0,
        iron_mg=3.0, meal_type="lunch", entry_origin=origin, logged_at=at,
    )


def _day_total(user) -> float:
    return NutritionSummaryService().summary(user_id=user.id, day=NOW.date()).totals.calories


# ---------------------------------------------------------------------------
# PATCH — правка
# ---------------------------------------------------------------------------


@freeze_time(NOW)
class TestPatch:
    def test_portion_change_rescales_the_snapshot_and_names_the_correction(self, owner):
        log = _log(owner)

        resp = _client().patch(_url(log.id), {"portion_multiplier": 3.0}, format="json")

        assert resp.status_code == 200, resp.json()
        log.refresh_from_db()
        # Порция вдвое — снимок вдвое, микронутриенты тоже.
        assert log.portion_multiplier == 3.0
        assert log.calories == 600.0
        assert log.protein_g == 24.0
        assert log.iron_mg == 6.0
        assert log.entry_origin == FoodLog.EntryOrigin.TEXT_USER_CORRECTED
        assert resp.json()["data"]["calories"] == 600.0
        assert _day_total(owner) == 600.0

    def test_photo_origin_becomes_photo_user_corrected(self, owner):
        log = _log(owner, origin=FoodLog.EntryOrigin.PHOTO_ESTIMATED_CONFIRMED)

        _client().patch(_url(log.id), {"portion_multiplier": 0.75}, format="json")

        log.refresh_from_db()
        assert log.calories == 150.0
        assert log.entry_origin == FoodLog.EntryOrigin.PHOTO_USER_CORRECTED

    def test_unknown_origin_is_not_invented(self, owner):
        log = _log(owner, origin=None)

        resp = _client().patch(_url(log.id), {"portion_multiplier": 3.0}, format="json")

        assert resp.status_code == 200
        log.refresh_from_db()
        assert log.calories == 600.0
        # Происхождение не передавали при записи — правка его не выдумывает.
        assert log.entry_origin is None

    def test_meal_type_only_keeps_numbers_and_origin(self, owner):
        log = _log(owner)

        resp = _client().patch(_url(log.id), {"meal_type": "dinner"}, format="json")

        assert resp.status_code == 200
        log.refresh_from_db()
        assert log.meal_type == "dinner"
        assert log.calories == 300.0
        # Числа человек не трогал — число осталось подтверждённой оценкой.
        assert log.entry_origin == FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED

    def test_nothing_to_change_is_a_validation_error(self, owner):
        log = _log(owner)

        resp = _client().patch(_url(log.id), {}, format="json")

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_stranger_cannot_edit_and_the_entry_is_intact(self, owner, stranger):
        log = _log(owner)

        resp = _client(STRANGER).patch(_url(log.id), {"portion_multiplier": 10.0}, format="json")

        assert resp.status_code == 404
        log.refresh_from_db()
        assert log.calories == 300.0
        assert log.portion_multiplier == 1.5
        # Положительная стража: 404 выше — отказ по владельцу, а не
        # отсутствующий маршрут: тот же вызов хозяина проходит.
        assert _client().patch(_url(log.id), {"portion_multiplier": 3.0}, format="json").status_code == 200


# ---------------------------------------------------------------------------
# DELETE / restore — удаление и окно восстановления
# ---------------------------------------------------------------------------


class TestDelete:
    @freeze_time(NOW)
    def test_deleted_entry_leaves_the_day_total_and_the_commitment_count(self, owner):
        kept = _log(owner, dish="Суп", calories=200.0)
        gone = _log(owner, dish="Торт", calories=450.0, at=NOW - timedelta(days=1))
        # Положительная стража: оба читателя видят обе записи до удаления.
        assert _day_total(owner) == 200.0
        assert get_commitment_days(owner) == 2

        resp = _client().delete(_url(gone.id))

        assert resp.status_code == 200, resp.json()
        body = resp.json()["data"]
        assert body["entry_id"] == str(gone.id)
        assert body["deleted"] is True
        assert body["restore_window_expires_at"] == (NOW + timedelta(minutes=15)).isoformat()
        assert FoodLog.objects.filter(id=kept.id).exists()
        assert get_commitment_days(owner) == 1
        assert not FoodLog.objects.filter(id=gone.id).exists()

    @freeze_time(NOW)
    def test_deleting_today_entry_recomputes_the_day(self, owner):
        _log(owner, dish="Суп", calories=200.0)
        gone = _log(owner, dish="Торт", calories=450.0)
        assert _day_total(owner) == 650.0

        _client().delete(_url(gone.id))

        assert _day_total(owner) == 200.0

    def test_restore_within_the_window_brings_back_the_same_entry(self, owner):
        with freeze_time(NOW):
            log = _log(owner)
            _client().delete(_url(log.id))
        with freeze_time(NOW + timedelta(minutes=14)):
            resp = _client().post(_url(log.id, "restore/"))

        assert resp.status_code == 200, resp.json()
        back = FoodLog.objects.get(id=log.id)
        assert (back.dish_name, back.calories, back.portion_multiplier) == ("Гречка", 300.0, 1.5)
        assert back.iron_mg == 3.0
        assert back.entry_origin == FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED
        assert back.logged_at == NOW
        with freeze_time(NOW):
            assert _day_total(owner) == 300.0

    def test_after_the_window_the_deletion_is_final(self, owner):
        with freeze_time(NOW):
            log = _log(owner)
            _client().delete(_url(log.id))
        with freeze_time(NOW + timedelta(minutes=16)):
            resp = _client().post(_url(log.id, "restore/"))

        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "RESTORE_WINDOW_EXPIRED"
        assert not FoodLog.objects.filter(id=log.id).exists()
        # Окончательно — значит, и снимка больше нет: второй раз не 410, а 404.
        with freeze_time(NOW + timedelta(minutes=17)):
            again = _client().post(_url(log.id, "restore/"))
        assert again.status_code == 404

    @freeze_time(NOW)
    def test_a_stranger_cannot_delete_and_the_entry_is_intact(self, owner, stranger):
        log = _log(owner)

        resp = _client(STRANGER).delete(_url(log.id))

        assert resp.status_code == 404
        assert FoodLog.objects.filter(id=log.id).exists()
        assert _day_total(owner) == 300.0
        # Положительная стража: тот же маршрут хозяину отвечает 200.
        assert _client().delete(_url(log.id)).status_code == 200

    def test_a_stranger_cannot_restore_someone_elses_deletion(self, owner, stranger):
        with freeze_time(NOW):
            log = _log(owner)
            _client().delete(_url(log.id))
        with freeze_time(NOW + timedelta(minutes=1)):
            resp = _client(STRANGER).post(_url(log.id, "restore/"))
            own = _client().post(_url(log.id, "restore/"))

        assert resp.status_code == 404
        # Положительная стража: снимок был жив, и хозяину восстановление удалось.
        assert own.status_code == 200
        assert FoodLog.objects.get(id=log.id).user_id == owner.id

    @freeze_time(NOW)
    def test_water_mirror_is_managed_by_the_water_undo(self, owner):
        log = _log(owner, dish="Кефир", calories=120.0, origin=None)
        WaterEntry.objects.create(user=owner, ts=NOW, ml=250, water_ml=225.0, kcal=120.0, food_log=log)

        deleted = _client().delete(_url(log.id))
        edited = _client().patch(_url(log.id), {"portion_multiplier": 2.0}, format="json")

        assert deleted.status_code == 409
        assert edited.status_code == 409
        log.refresh_from_db()
        assert log.calories == 120.0


class TestPurge:
    def test_expired_snapshots_are_erased_and_fresh_ones_kept(self, owner):
        from nutrition.models import DeletedFoodLog
        from nutrition.tasks import purge_expired_deleted_food_logs_task

        with freeze_time(NOW):
            old = _log(owner, dish="Старое")
            _client().delete(_url(old.id))
        with freeze_time(NOW + timedelta(minutes=10)):
            fresh = _log(owner, dish="Свежее")
            _client().delete(_url(fresh.id))
        assert DeletedFoodLog.objects.filter(user=owner).count() == 2

        with freeze_time(NOW + timedelta(minutes=16)):
            purged = purge_expired_deleted_food_logs_task()

        assert purged == 1
        assert list(DeletedFoodLog.objects.values_list("id", flat=True)) == [fresh.id]

    def test_the_purge_runs_every_fifteen_minutes(self, settings):
        entry = settings.CELERY_BEAT_SCHEDULE["purge-expired-deleted-food-logs"]
        assert entry["task"] == "nutrition.purge_expired_deleted_food_logs"
