"""Мультивыбор результата на шаге ``feeling`` (DRF-1759, карта C02 — M2).

Макет C02.2: несколько результатов, «Продолжить» при ≥1. Механика
режима ``multi`` построена раньше (DRF-1746: форма ответа, хранение
массива, подпись «Уже учла»); здесь шаг ``feeling`` переводится в этот
режим.

Что заперто:

- документ спрашивает ``feeling`` как ``multi`` с вариантами шага и
  «Не знаю» отдельной ролью;
- два результата — одно тело, хранятся в порядке вариантов шага, не
  тапов; одиночный ключ и пустой массив — 400, строки нет;
- «Не знаю» по-прежнему отвечает одним ключом и заменяет массив;
- второй проход подтверждает массив: ``known_value.option_keys`` и
  подпись через запятую, «Да» копирует массив;
- положительная стража: ``area`` остаётся одним ответом (массив — 400,
  ключ — 200).
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.models import GoalAnketaAnswer, GoalAnketaRun
from services.models import GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1759"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:feeling-multi"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001759", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)]


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _answer(api, step: str, **kwargs):
    return api.post(
        SELECT_URL, {"answer": {"step": step, **kwargs}, "source_channel": "miniapp"},
        format="json",
    )


def _current(doc) -> dict:
    items = [m for m in doc["missing"] if m["kind"] == anketa.MISSING_GOAL_ANKETA]
    assert len(items) == 1, doc["missing"]
    return items[0]


def _to_feeling(api) -> dict:
    assert _answer(api, anketa.GOAL_STEP_KEY, option_key="relax").status_code == 200
    resp = _answer(api, AREA.key, option_key="face")
    assert resp.status_code == 200, resp.content
    return _current(resp.json()["data"])


def _feeling_row():
    return GoalAnketaAnswer.objects.filter(step_key=FEELING.key).order_by("-created_at").first()


@pytest.mark.django_db
class TestFeelingIsMulti:
    def test_the_document_asks_feeling_as_multi(self, customer, token, goal_options):
        item = _to_feeling(_api())
        assert item["step"] == FEELING.key
        assert item["mode"] == anketa.MODE_MULTI
        regular = [o["key"] for o in item["options"] if o.get("role") != "escape"]
        assert regular == [k for k, _ in FEELING.options]
        assert [o["key"] for o in item["options"] if o.get("role") == "escape"] == [
            anketa.UNKNOWN_OPTION_KEY
        ]

    def test_two_results_are_one_body_in_step_order(self, customer, token, goal_options):
        api = _api()
        _to_feeling(api)
        # Тапнули «Спокойнее», потом «Отдохнувшей» — храним в порядке шага.
        resp = _answer(api, FEELING.key, option_keys=["calmer", "rested"])
        assert resp.status_code == 200, resp.content
        row = _feeling_row()
        assert row.option_keys == ["rested", "calmer"]
        assert row.option_key is None
        # Шаг последний — проход закрыт, цель сформирована одним ответом.
        assert not GoalAnketaRun.objects.filter(completed_at__isnull=True).exists()

    def test_a_single_key_is_refused(self, customer, token, goal_options):
        api = _api()
        _to_feeling(api)
        resp = _answer(api, FEELING.key, option_key="calmer")
        assert resp.status_code == 400, resp.content
        assert _feeling_row() is None

    def test_an_empty_array_is_refused(self, customer, token, goal_options):
        api = _api()
        _to_feeling(api)
        resp = _answer(api, FEELING.key, option_keys=[])
        assert resp.status_code == 400, resp.content
        assert _feeling_row() is None

    def test_dont_know_still_answers_with_one_key(self, customer, token, goal_options):
        api = _api()
        _to_feeling(api)
        resp = _answer(api, FEELING.key, option_key=anketa.UNKNOWN_OPTION_KEY)
        assert resp.status_code == 200, resp.content
        row = _feeling_row()
        assert (row.option_key, row.option_keys) == (anketa.UNKNOWN_OPTION_KEY, [])


@pytest.mark.django_db
class TestSecondPassConfirmsTheArray:
    def test_known_value_carries_the_array_and_yes_copies_it(
        self, customer, token, goal_options
    ):
        api = _api()
        _to_feeling(api)
        assert _answer(api, FEELING.key, option_keys=["rested", "calmer"]).status_code == 200

        resp = api.post(
            SELECT_URL, {"intent": "start_anketa", "source_channel": "miniapp"}, format="json",
        )
        assert resp.status_code == 200, resp.content
        assert _answer(api, anketa.GOAL_STEP_KEY, option_key="relax").status_code == 200
        resp = _answer(api, AREA.key, confirm=True)
        assert resp.status_code == 200, resp.content

        item = _current(resp.json()["data"])
        assert item["step"] == FEELING.key
        assert item["mode"] == anketa.MODE_CONFIRM
        assert item["answer_mode"] == anketa.MODE_MULTI
        assert item["known_value"]["option_keys"] == ["rested", "calmer"]
        assert item["known_value"]["label"] == "Отдохнувшей, Спокойнее"

        assert _answer(api, FEELING.key, confirm=True).status_code == 200
        assert _feeling_row().option_keys == ["rested", "calmer"]


@pytest.mark.django_db
class TestAreaStaysSingle:
    def test_area_is_one_answer(self, customer, token, goal_options):
        api = _api()
        resp = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax")
        item = _current(resp.json()["data"])
        assert item["step"] == AREA.key
        assert item["mode"] == anketa.MODE_SINGLE
        assert _answer(api, AREA.key, option_keys=["face", "hair"]).status_code == 400
        assert _answer(api, AREA.key, option_key="face").status_code == 200
