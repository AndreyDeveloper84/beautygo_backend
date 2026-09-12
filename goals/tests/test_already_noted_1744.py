"""Блок «Уже учла» и пересмотр ответа (DRF-1744).

Макет C03 (DRF-1178): человек видит, что его услышали, и любой
показанный факт может исправить. До этого среза ответы ``area`` /
``feeling`` писались и никем не читались, а ответ не на ожидаемый шаг
получал 409 — «Изменить» было невозможно.

Что заперто:

- ``known.anketa`` — ответы ОТКРЫТОГО прохода, в порядке шагов, каждая
  строка со своими ``options`` и ``revisable`` (экран порядок не
  вычисляет и варианты не знает);
- ``revise: true`` меняет уже данный ответ и не сдвигает проход;
- пересмотр неотвеченного шага — тот же 409, что и раньше (положительная
  стража: старый отказ не ослаблен);
- финальный шаг пересмотру не подлежит — 400, не 409 и не запись;
- завершённый проход ничего не показывает и ничего не даёт править.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import build_decision_context
from goals.models import GoalAnketaAnswer, GoalAnketaRun
from services.models import GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1744"
CTX_URL = "/api/v1/internal/me/decision-context/"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:already-noted"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001744", is_proxy=True,
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


def _answer(api, step: str, *, revise: bool = False, **kwargs):
    body = {"answer": {"step": step, **kwargs}, "source_channel": "miniapp"}
    if revise:
        body["answer"]["revise"] = True
    return api.post(SELECT_URL, body, format="json")


@pytest.mark.django_db
class TestKnownAnketa:
    def test_empty_before_the_first_answer(self, customer, token):
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["known"]["anketa"] == []

    def test_each_answer_becomes_a_renderable_row_in_step_order(
        self, customer, token,
    ):
        api = _api()
        _answer(api, AREA.key, option_key=AREA.options[1][0])
        doc = _answer(api, FEELING.key, option_key=FEELING.options[0][0]).json()["data"]

        rows = doc["known"]["anketa"]
        assert [r["step"] for r in rows] == [AREA.key, FEELING.key]
        area_row = rows[0]
        assert area_row == {
            "step": AREA.key,
            "prompt": AREA.prompt,
            "option_key": AREA.options[1][0],
            "label": AREA.options[1][1],
            "options": [{"key": k, "label": lbl} for k, lbl in AREA.options],
            "revisable": True,
        }
        # Голый вопрос, без пометки о влиянии: блок говорит «что ты
        # сказал», не «зачем спросили».
        assert anketa.NO_INFLUENCE_NOTE not in area_row["prompt"]

    def test_row_order_is_the_step_order_not_the_answer_order(self, customer, token):
        run = GoalAnketaRun.objects.create(client=customer)
        GoalAnketaAnswer.objects.create(run=run, step_key=FEELING.key, option_key=FEELING.options[2][0])
        GoalAnketaAnswer.objects.create(run=run, step_key=AREA.key, option_key=AREA.options[0][0])
        rows = build_decision_context(customer)["known"]["anketa"]
        assert [r["step"] for r in rows] == [AREA.key, FEELING.key]

    def test_answer_to_a_retired_step_is_not_shown(self, customer, token):
        """Шаг, снятый из ANKETA_STEPS, нечем исправить — его не показываем."""
        run = GoalAnketaRun.objects.create(client=customer)
        GoalAnketaAnswer.objects.create(run=run, step_key="retired", option_key="x")
        GoalAnketaAnswer.objects.create(run=run, step_key=AREA.key, option_key=AREA.options[0][0])
        rows = build_decision_context(customer)["known"]["anketa"]
        assert [r["step"] for r in rows] == [AREA.key]

    def test_completed_run_shows_nothing(self, customer, token, goal_options):
        api = _api()
        for step in anketa.ANKETA_STEPS:
            _answer(api, step.key, option_key=step.options[0][0])
        doc = _answer(api, anketa.FINAL_STEP_KEY, option_key="relax").json()["data"]
        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["known"]["anketa"] == []


@pytest.mark.django_db
class TestRevise:
    def test_revise_changes_the_answer_and_keeps_the_pass_where_it_was(
        self, customer, token,
    ):
        api = _api()
        _answer(api, AREA.key, option_key=AREA.options[0][0])
        before = api.get(CTX_URL).json()["data"]
        assert before["missing"][0]["step"] == FEELING.key

        resp = _answer(api, AREA.key, revise=True, option_key=AREA.options[3][0])
        assert resp.status_code == 200, resp.content
        doc = resp.json()["data"]

        assert doc["known"]["anketa"][0]["option_key"] == AREA.options[3][0]
        assert doc["known"]["anketa"][0]["label"] == AREA.options[3][1]
        # Проход не сдвинулся: следующий вопрос тот же.
        assert doc["missing"][0]["step"] == FEELING.key
        assert GoalAnketaAnswer.objects.filter(step_key=AREA.key).count() == 1

    def test_revising_an_unanswered_step_is_the_same_409_as_before(
        self, customer, token,
    ):
        """Положительная стража: пересмотр не ослабляет защиту порядка."""
        api = _api()
        _answer(api, AREA.key, option_key=AREA.options[0][0])
        resp = _answer(api, FEELING.key, revise=True, option_key=FEELING.options[0][0])
        assert resp.status_code == 409, resp.content
        err = resp.json()["error"]
        assert err["code"] == "ANKETA_STEP_MISMATCH"
        assert err["details"] == {"expected_step": FEELING.key}
        assert not GoalAnketaAnswer.objects.filter(step_key=FEELING.key).exists()

    def test_revise_without_an_open_run_is_409_and_writes_nothing(
        self, customer, token,
    ):
        resp = _api().post(
            SELECT_URL,
            {"answer": {"step": AREA.key, "option_key": AREA.options[0][0], "revise": True},
             "source_channel": "miniapp"},
            format="json",
        )
        assert resp.status_code == 409, resp.content
        assert GoalAnketaRun.objects.count() == 0
        assert GoalAnketaAnswer.objects.count() == 0

    def test_final_step_cannot_be_revised(self, customer, token, goal_options):
        """Цель меняют выбором цели, не пересмотром ответа: 400, не 409."""
        api = _api()
        for step in anketa.ANKETA_STEPS:
            _answer(api, step.key, option_key=step.options[0][0])
        resp = _answer(api, anketa.FINAL_STEP_KEY, revise=True, option_key="relax")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_option_on_revise_is_validation_error(self, customer, token):
        api = _api()
        _answer(api, AREA.key, option_key=AREA.options[0][0])
        resp = _answer(api, AREA.key, revise=True, option_key="nope")
        assert resp.status_code == 400, resp.content
        assert GoalAnketaAnswer.objects.get(step_key=AREA.key).option_key == AREA.options[0][0]

    def test_plain_answer_without_revise_still_refuses_out_of_order(
        self, customer, token,
    ):
        """Старый путь не изменился: без флага ответ не по порядку — 409."""
        api = _api()
        _answer(api, AREA.key, option_key=AREA.options[0][0])
        resp = _answer(api, AREA.key, option_key=AREA.options[1][0])
        assert resp.status_code == 409
        assert GoalAnketaAnswer.objects.get(step_key=AREA.key).option_key == AREA.options[0][0]
