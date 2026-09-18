"""Избранные блюда — серверный источник под субъектом (DRF-2092, дневник F12).

Что стережётся:

* **ручки есть и живут под субъектом** — ``X-External-User-ID`` решает,
  чьё избранное читается, пишется и удаляется; до этого тикета трёх ручек
  не было вовсе (замер на ``dev 06ff4335``: любой запрос — 404);
* **переживает переустановку** — источник на сервере, не на устройстве:
  тот же внешний идентификатор из свежей сессии видит тот же список;
* **изоляция** — чужой список пуст, чужое удаление — 404, строка владельца
  жива после чужой попытки; «нет» здесь означает «не твоё», а не «нет
  такой», чтобы по коду ответа нельзя было перебирать чужие id;
* **удаление мягкое** — строка скрыта из списка, а не стёрта: возврат и
  аудит остаются возможны; стирает её только erasure-исполнитель вместе
  с личностью (``users.deletion_executor``, реестр ``DELETE``);
* **повтор — не дубль** — то же блюдо с той же порцией второй раз
  возвращает существующую строку.

Красное до правки объявлено поимённо до прогона: 6 красных, все — 404
«ручки нет»; зелёных контролей «до» нет по построению. Узел про erasure до
появления модели не измерим (ошибка импорта, не красный) — назван так.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-2092"
OWNER = "bot:2092"
STRANGER = "bot:2093"
LIST_URL = "/api/v1/nutrition/internal/saved-meals/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def owner(db) -> User:
    return User.objects.create(username=OWNER, role="client", is_proxy=True)


@pytest.fixture
def stranger(db) -> User:
    return User.objects.create(username=STRANGER, role="client", is_proxy=True)


def _client(external_id: str = OWNER) -> APIClient:
    """Свежий клиент = свежая сессия: между вызовами на устройстве ничего нет."""
    c = APIClient()
    c.defaults["HTTP_X_SERVICE_TOKEN"] = SERVICE_TOKEN
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return c


def _url(meal_id) -> str:
    return f"{LIST_URL}{meal_id}/"


BORSCH = {
    "dish_name": "Борщ",
    "portion_g": 250,
    "calories": 125.0,
    "protein_g": 5.0,
    "fat_g": 7.5,
    "carbs_g": 10.0,
}


def _save(client: APIClient, **body):
    return client.post(LIST_URL, {**BORSCH, **body}, format="json")


def _items(client: APIClient) -> list[dict]:
    resp = client.get(LIST_URL)
    assert resp.status_code == 200, resp.content[:400]
    return resp.json()["data"]["items"]


class TestTheServerIsTheSource:
    def test_save_then_list_returns_it_from_the_server(self, owner) -> None:
        resp = _save(_client())

        assert resp.status_code == 201, resp.content[:400]
        saved = resp.json()["data"]
        assert saved["dish_name"] == "Борщ"
        assert saved["portion_g"] == 250.0
        assert saved["calories"] == 125.0

        items = _items(_client())
        assert [i["id"] for i in items] == [saved["id"]]
        assert items[0]["portion_g"] == 250.0

    def test_save_from_a_food_log_snapshots_the_entry(self, owner) -> None:
        """«Сохранить в избранное» из записи: снимок берётся из записи субъекта."""
        from datetime import datetime, timezone as dt_tz

        from nutrition.models import FoodLog

        log = FoodLog.objects.create(
            user=owner, dish_name="Гречка", portion_multiplier=1.5,
            calories=300.0, protein_g=12.0, fat_g=6.0, carbs_g=40.0,
            meal_type="lunch", entry_origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED,
            logged_at=datetime(2026, 9, 15, 12, 0, tzinfo=dt_tz.utc),
        )

        resp = _client().post(LIST_URL, {"food_log_id": str(log.id)}, format="json")

        assert resp.status_code == 201, resp.content[:400]
        saved = resp.json()["data"]
        assert saved["dish_name"] == "Гречка"
        # 1.5 × базовые 100 г — порция в граммах, как её видит человек.
        assert saved["portion_g"] == 150.0
        assert saved["calories"] == 300.0
        assert saved["source_food_log_id"] == str(log.id)

    def test_the_list_survives_a_reinstall(self, owner) -> None:
        """Переустановка = тот же человек, новая сессия, пустое устройство."""
        first_install = _client()
        saved = _save(first_install).json()["data"]

        after_reinstall = _client()  # ничего от первой сессии не унаследовано

        assert [i["id"] for i in _items(after_reinstall)] == [saved["id"]]


class TestIsolation:
    def test_a_stranger_sees_nothing_and_cannot_delete(self, owner, stranger) -> None:
        saved = _save(_client(OWNER)).json()["data"]

        assert _items(_client(STRANGER)) == []
        resp = _client(STRANGER).delete(_url(saved["id"]))
        assert resp.status_code == 404
        # Строка владельца жива после чужой попытки.
        assert [i["id"] for i in _items(_client(OWNER))] == [saved["id"]]

    def test_a_stranger_cannot_save_from_someone_elses_food_log(self, owner, stranger) -> None:
        from datetime import datetime, timezone as dt_tz

        from nutrition.models import FoodLog

        log = FoodLog.objects.create(
            user=owner, dish_name="Гречка", portion_multiplier=1.0,
            calories=200.0, meal_type="lunch",
            entry_origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED,
            logged_at=datetime(2026, 9, 15, 12, 0, tzinfo=dt_tz.utc),
        )

        resp = _client(STRANGER).post(LIST_URL, {"food_log_id": str(log.id)}, format="json")

        assert resp.status_code == 404
        assert _items(_client(STRANGER)) == []


class TestDeleteAndRepeat:
    def test_delete_is_soft_and_hides_from_the_list(self, owner) -> None:
        from nutrition.models import SavedMeal

        saved = _save(_client()).json()["data"]

        resp = _client().delete(_url(saved["id"]))

        assert resp.status_code == 200, resp.content[:400]
        assert _items(_client()) == []
        row = SavedMeal.objects.get(pk=saved["id"])
        assert row.deleted_at is not None  # скрыта, не стёрта
        # Второе удаление — уже «нет такой»: скрытая строка не адресуется.
        assert _client().delete(_url(saved["id"])).status_code == 404

    def test_saving_the_same_dish_twice_returns_the_existing_row(self, owner) -> None:
        first = _save(_client())
        second = _save(_client())

        assert first.status_code == 201
        assert second.status_code == 200, second.content[:400]
        assert second.json()["data"]["id"] == first.json()["data"]["id"]
        assert len(_items(_client())) == 1


class TestErasure:
    def test_saved_meal_rows_are_erased_with_the_person(self, owner) -> None:
        """Реестр удаления знает таблицу, шаг исполнителя её стирает — включая
        мягко удалённые: «скрыта» не значит «забыта»."""
        from nutrition.models import SavedMeal
        from users.deletion_executor import DELETE, _erase_catalog, _residue

        assert "nutrition.SavedMeal.user" in DELETE
        saved = _save(_client()).json()["data"]
        _client().delete(_url(saved["id"]))  # мягко удалённая — тоже личность
        _save(_client(), dish_name="Омлет")
        assert SavedMeal.objects.filter(user=owner).count() == 2

        _erase_catalog(owner)

        assert SavedMeal.objects.filter(user=owner).count() == 0
        assert _residue(owner).get("nutrition.SavedMeal", 0) == 0
