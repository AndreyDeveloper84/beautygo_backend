"""Подтверждение известного вместо переспроса (DRF-1745, макет C03.3).

Повторный проход `start_anketa` начинал с нуля — человек, уже ответивший
на `area`/`feeling`, отвечал снова. Теперь сужающий шаг, на который есть
ответ в завершённом проходе, приходит подтверждением.

Что заперто:

- второй проход → первый сужающий шаг `mode=confirm` с прошлым значением
  (`known_value`, `prompt` «Раньше ты выбирала «X». Всё ещё так?»,
  `question` — обычный вопрос, `answer_mode` — чем отвечать на
  «Изменилось»), а не пустой `area`;
- «Да» (`confirm: true`) копирует прошлое значение в новый проход и ведёт
  дальше; «Изменилось» — обычный ответ шага;
- «Не знаю» из прошлого прохода не предзаполняется: шаг задаётся заново;
- положительная стража: первый проход без прошлого не меняется, `confirm`
  на нём — 400, не запись; шаг цели подтверждением не приходит.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import build_decision_context
from goals.models import GoalAnketaAnswer, GoalAnketaRun
from services.models import GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1745"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:confirm-known"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001745", is_proxy=True,
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


def _complete_first_pass(api, *, feeling: str = "calmer") -> None:
    """Проход целиком: цель → area=face → feeling."""
    for step, key in (
        (anketa.GOAL_STEP_KEY, "relax"), (AREA.key, "face"), (FEELING.key, feeling),
    ):
        resp = _answer(api, step, option_key=key)
        assert resp.status_code == 200, resp.content
    assert not GoalAnketaRun.objects.filter(completed_at__isnull=True).exists()


def _second_pass_to_area(api) -> dict:
    """Повторный вход и ответ на цель — на очереди первый сужающий шаг."""
    resp = api.post(
        SELECT_URL, {"intent": "start_anketa", "source_channel": "miniapp"}, format="json",
    )
    assert resp.status_code == 200, resp.content
    goal_item = _current(resp.json()["data"])
    # Цель — первый шаг и никогда не «подтверждение».
    assert goal_item["step"] == anketa.GOAL_STEP_KEY
    assert goal_item["mode"] == anketa.MODE_SINGLE
    resp = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax")
    assert resp.status_code == 200, resp.content
    return _current(resp.json()["data"])


@pytest.mark.django_db
class TestSecondPassConfirms:
    def test_first_pass_has_no_confirmation(self, customer, token, goal_options):
        api = _api()
        resp = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax")
        item = _current(resp.json()["data"])
        assert item["step"] == AREA.key
        assert item["mode"] == anketa.MODE_SINGLE
        assert "known_value" not in item and "question" not in item

    def test_second_pass_asks_to_confirm_the_previous_answer(
        self, customer, token, goal_options
    ):
        api = _api()
        _complete_first_pass(api)
        item = _second_pass_to_area(api)
        assert item["step"] == AREA.key
        assert item["mode"] == anketa.MODE_CONFIRM
        assert item["answer_mode"] == anketa.MODE_SINGLE
        assert item["known_value"]["option_key"] == "face"
        assert item["known_value"]["label"] == "Лицо и кожа"
        assert item["prompt"] == "Раньше ты выбирала «Лицо и кожа». Всё ещё так?"
        # Обычный вопрос — рядом, для «Изменилось»; варианты — как у шага.
        assert item["question"] == anketa.shown_prompt(AREA, "relax")
        assert [o["key"] for o in item["options"]] == [k for k, _ in AREA.options]

    def test_yes_copies_the_value_and_moves_on(self, customer, token, goal_options):
        api = _api()
        _complete_first_pass(api)
        _second_pass_to_area(api)
        resp = _answer(api, AREA.key, confirm=True)
        assert resp.status_code == 200, resp.content
        open_run = GoalAnketaRun.objects.get(completed_at__isnull=True)
        assert open_run.answers.get(step_key=AREA.key).option_key == "face"
        nxt = _current(resp.json()["data"])
        assert nxt["step"] == FEELING.key
        assert nxt["mode"] == anketa.MODE_CONFIRM
        assert nxt["known_value"]["option_key"] == "calmer"
        # «Уже учла» в новом проходе — подтверждённое, известным считается.
        known = build_decision_context(customer)["known"]["anketa"]
        assert [(r["step"], r["option_key"], r["unknown"]) for r in known] == [
            (AREA.key, "face", False)
        ]

    def test_changed_is_an_ordinary_answer(self, customer, token, goal_options):
        api = _api()
        _complete_first_pass(api)
        _second_pass_to_area(api)
        resp = _answer(api, AREA.key, option_key="hands")
        assert resp.status_code == 200, resp.content
        open_run = GoalAnketaRun.objects.get(completed_at__isnull=True)
        assert open_run.answers.get(step_key=AREA.key).option_key == "hands"

    def test_previous_dont_know_is_asked_again_not_confirmed(
        self, customer, token, goal_options
    ):
        api = _api()
        _complete_first_pass(api, feeling=anketa.UNKNOWN_OPTION_KEY)
        _second_pass_to_area(api)
        assert _answer(api, AREA.key, confirm=True).status_code == 200
        doc = build_decision_context(customer)
        item = _current(doc)
        assert item["step"] == FEELING.key
        assert item["mode"] == FEELING.mode
        assert "known_value" not in item
        resp = _answer(api, FEELING.key, confirm=True)
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaRun.objects.get(completed_at__isnull=True).answers.filter(
            step_key=FEELING.key
        ).exists()

    def test_confirm_on_the_first_pass_is_refused(self, customer, token, goal_options):
        api = _api()
        assert _answer(api, anketa.GOAL_STEP_KEY, option_key="relax").status_code == 200
        resp = _answer(api, AREA.key, confirm=True)
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaAnswer.objects.filter(step_key=AREA.key).exists()

    def test_confirm_with_revise_is_refused(self, customer, token, goal_options):
        api = _api()
        _complete_first_pass(api)
        _second_pass_to_area(api)
        resp = _answer(api, AREA.key, confirm=True, revise=True)
        assert resp.status_code == 400, resp.content
