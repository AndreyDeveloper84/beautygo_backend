"""Выгрузка по ст. 14 (C5.1) несёт то, что каталог запомнил о человеке (DRF-2214, PR-2).

«Забудь всё» стирает цели, план, профиль питания, дневник с фото и историю
подсказок (``users.forget_all_catalog.REMEMBERED_SCOPE``), а выгрузка — то, по
чему человек узнаёт, ЧТО о нём хранится, — несла только профиль, личный
профиль предпочтений и профиль мастера. Стереть можно было то, чего человек
в своей выгрузке не видел.

Правило, которое держит сторож: **что стирается — то выгружается.** Каждое
слово словаря стирания — раздел выгрузки с тем же именем; бот вкладывает
ответ C5.1 в свою выгрузку целиком (ai-bot-platform
``apps/identity/services/privacy.py`` — раздел ``ayla``).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from goals.models import ClientGoal
from nutrition.models import FoodScan, NutritionProfile
from users.tests.test_forget_all_catalog_2214 import (
    ANSWER_TEXT,
    GOAL_TEXT,
    _seed_remembered,
)
from users.tests.test_forget_all_diary_2214 import _seed_diary
from users.tests.test_memory_erasure_matrix import (  # noqa: F401 — фикстуры по имени
    _internal,
    _set_token,
    user,
)

User = get_user_model()
pytestmark = pytest.mark.django_db

EXPORT_URL = "/api/v1/internal/users/{user_id}/personal-data/export/"


def _carriers(needle: str, value, path: str = "выгрузка") -> list[str]:
    """Места, откуда подстрока попала в выгрузку.

    Предикат тот же, что у ``needle not in repr(...)``, но падение называет
    носителя: без имени поля красный сторож приватности требует перебора
    файлов, а перебор стоит прогона (DRF-2428).
    """
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if needle in repr(key):
                found.append(f"{path}: ключ {key!r}")
            found += _carriers(needle, item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += _carriers(needle, item, f"{path}[{index}]")
    elif needle in repr(value):
        found.append(f"{path} = {value!r}")
    return found


def _export(u) -> dict:
    resp = _internal().get(EXPORT_URL.format(user_id=u.pk))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


@pytest.fixture
def remembered(user):  # noqa: F811 — фикстура по имени
    _seed_remembered(user)
    scan = _seed_diary(user)
    return user, scan


class TestWhatIsErasedIsExported:
    def test_every_erased_group_is_an_export_section(self, user) -> None:  # noqa: F811
        """Сторож правила: слово словаря стирания без раздела выгрузки — красное."""
        from users.forget_all_catalog import SCOPE_ORDER

        data = _export(user)

        assert "profile" in data  # наличие: выгрузка та самая
        missing = [word for word in SCOPE_ORDER if word not in data]
        assert missing == [], missing


class TestTheSectionsCarryTheValues:
    def test_goals_carry_the_verbatim_words(self, remembered) -> None:
        """Дословная цель и свободный ответ анкеты — то, что человек должен увидеть."""
        u, _ = remembered

        goals = _export(u)["goals"]

        assert [g["goal_text"] for g in goals["goals"]] == [GOAL_TEXT]
        answers = [a["answer_text"] for run in goals["anketa_runs"] for a in run["answers"]]
        assert answers == [ANSWER_TEXT]

    def test_the_wellness_plan_is_there(self, remembered) -> None:
        u, _ = remembered

        plan = _export(u)["wellness_plan"]

        assert [o["statement_text"] for o in plan["desired_outcomes"]] == ["хочу сбросить вес"]
        assert len(plan["plans"]) == 1
        assert [a["action_type"] for a in plan["plans"][0]["actions"]] == ["log_food"]
        assert len(plan["progress_observations"]) == 2

    def test_the_nutrition_profile_is_there(self, remembered) -> None:
        u, _ = remembered

        profile = _export(u)["nutrition_profile"]

        assert profile["weight_kg"] == 60
        assert profile["height_cm"] == 165
        assert "health_flags" in profile

    def test_the_food_diary_is_there_with_the_photo(self, remembered) -> None:
        u, scan = remembered

        diary = _export(u)["food_diary"]

        assert [x["dish_name"] for x in diary["food_logs"]] == ["Борщ"]
        assert [x["dish_name"] for x in diary["food_scans"]] == ["Борщ"]
        assert diary["food_scans"][0]["image_url"].endswith(scan.image.name.rsplit("/", 1)[-1])
        assert len(diary["water_entries"]) == 2  # и мягко удалённая
        assert len(diary["water_logs"]) == 1
        assert sorted(x["dish_name"] for x in diary["saved_meals"]) == ["борщ", "омлет"]
        assert [x["snapshot"]["dish_name"] for x in diary["deleted_food_logs"]] == ["Окрошка"]

    def test_the_hint_history_is_there(self, remembered) -> None:
        u, _ = remembered

        hints = _export(u)["shown_hints"]

        assert [h["nutrition_trigger"] for h in hints] == ["low_vitamin_d"]


class TestTheWholeSubjectAndNobodyElse:
    def test_a_linked_proxy_carries_its_own_sections(self, user) -> None:  # noqa: F811
        proxy = User.objects.create(
            username="bot:max:exp2214-proxy", role="client", is_proxy=True, linked_user=user
        )
        _seed_remembered(proxy)

        data = _export(user)

        linked = {item["external_user_id"]: item for item in data["linked_identities"]}
        assert [g["goal_text"] for g in linked[proxy.username]["goals"]["goals"]] == [GOAL_TEXT]
        # Строки прокси — в его разделе, не в разделе аккаунта.
        assert data["goals"]["goals"] == []

    def test_a_neighbours_data_is_not_exported(self, remembered) -> None:
        u, _ = remembered
        neighbour = User.objects.create_user(
            username="exp2214_neighbour", password="x", role="client", phone="+79995559004"
        )
        ClientGoal.objects.create(
            client=neighbour, goal_key="skin", source_channel="bot", goal_text="соседская цель"
        )

        data = _export(u)

        assert [g["goal_text"] for g in data["goals"]["goals"]] == [GOAL_TEXT]
        assert "соседская цель" not in repr(data)

    def test_a_neighbours_scan_and_its_telemetry_are_not_exported(self, user) -> None:  # noqa: F811
        """Сосед со сканом: в дневнике нет ни его блюда, ни его телеметрии.

        Соседний узел выше держит эту границу на целях; дневник ею не закрыт,
        а именно он — ось DRF-2428: подозревали, что в `food_scans[0]` попадает
        чужой скан. Чтением это опровергнуто (выгрузка фильтрует по человеку),
        узел делает то же утверждение проверяемым.
        """
        _seed_diary(user)
        neighbour = User.objects.create_user(
            username="exp2214_scan_neighbour", password="x", role="client", phone="+79995559005"
        )
        theirs = _seed_diary(neighbour, tag="-neighbour")
        theirs.dish_name = "соседский борщ"
        theirs.provider_cost_usd = 0.9876
        theirs.raw_response = {"dish": "соседский борщ"}
        theirs.save(update_fields=["dish_name", "provider_cost_usd", "raw_response"])

        diary = _export(user)["food_diary"]

        assert len(diary["food_scans"]) == 1
        assert diary["food_scans"][0]["dish_name"] == "Борщ"
        assert _carriers("0.9876", diary) == [], _carriers("0.9876", diary)
        assert "соседский борщ" not in repr(diary)


class TestAnExportCreatesNothing:
    def test_empty_sections_for_a_person_with_nothing(self, user) -> None:  # noqa: F811
        """Пусто — пустыми разделами, и выгрузка ничего не создаёт (как у личного профиля)."""
        data = _export(user)

        assert data["goals"] == {"goals": [], "anketa_runs": []}
        assert data["nutrition_profile"] is None
        assert data["food_diary"]["food_logs"] == []
        assert data["shown_hints"] == []
        assert not NutritionProfile.objects.filter(user=user).exists()
        assert not FoodScan.objects.filter(user=user).exists()


class TestEveryFieldIsDecided:
    def test_every_field_of_every_exported_model_is_classified(self) -> None:
        """Новое поле модели без решения — красное: ни молча мимо выгрузки, ни молча в неё."""
        from django.apps import apps

        from users.remembered_export import FIELDS

        assert FIELDS  # наличие
        for label, (exported, excluded) in FIELDS.items():
            names = {f.name for f in apps.get_model(label)._meta.concrete_fields}
            assert set(exported) | set(excluded) == names, (
                label, sorted(names - set(exported) - set(excluded)),
                sorted((set(exported) | set(excluded)) - names),
            )
            assert not set(exported) & set(excluded), label
            assert all(reason.strip() for reason in excluded.values()), label

    def test_every_erased_model_has_a_field_decision(self) -> None:
        """Стык со стиранием: каждая модель словаря стирания выгружается или объявлена с причиной."""
        from users.forget_all_catalog import REMEMBERED_SCOPE
        from users.remembered_export import DECLARED_NOT_EXPORTED, FIELDS

        erased = set(REMEMBERED_SCOPE) - {"files"}
        assert erased  # наличие
        decided = set(FIELDS) | set(DECLARED_NOT_EXPORTED)
        assert erased <= decided, sorted(erased - decided)
        assert not set(FIELDS) & set(DECLARED_NOT_EXPORTED)
        assert all(reason.strip() for reason in DECLARED_NOT_EXPORTED.values())


class TestTheScanRecognition:
    def test_the_raw_model_response_is_exported_and_marked(self, user) -> None:  # noqa: F811
        """Слово главного окна: сырой ответ модели о фото выгружается, с пометкой, что он сырой."""
        scan = _seed_diary(user)
        scan.raw_response = {"dish": "борщ со сметаной", "p": 0.91}
        scan.provider_cost_usd = 0.0123
        scan.save(update_fields=["raw_response", "provider_cost_usd"])

        diary = _export(user)["food_diary"]

        assert diary["food_scans"][0]["raw_model_response"] == {"dish": "борщ со сметаной", "p": 0.91}
        assert "сырой ответ модели" in diary["notes"]["raw_model_response"]
        # Телеметрия провайдера — не данные о человеке, не выгружается.
        assert "provider_cost_usd" not in diary["food_scans"][0]
        assert _carriers("0.0123", diary) == [], _carriers("0.0123", diary)
        assert "0.0123" not in repr(diary)

    def test_service_keys_do_not_leave(self, remembered) -> None:
        """Ключ повтора — служебный: в выгрузке его нет, а сама запись есть."""
        from nutrition.models import FoodLog

        u, _ = remembered
        key = FoodLog.objects.get(user=u).idempotency_key

        diary = _export(u)["food_diary"]

        assert [x["dish_name"] for x in diary["food_logs"]] == ["Борщ"]  # наличие
        assert key not in repr(diary)
        assert "idempotency_key" not in diary["food_logs"][0]
