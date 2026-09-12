"""«Не знаю» — полноценный ответ (DRF-1747, макет C03 P12).

Что заперто:

- в документе шаг с ``escape`` несёт опцию ``unknown`` последней и с ролью
  ``escape``; у обычных вариантов роли нет, шаг без ``escape`` не меняется
  (положительная стража), на выборе цели «Не знаю» нет;
- ответ ``unknown`` сохраняется как durable-ответ шага (не пропуск): шаг в
  проходе не задаётся повторно, следующий вопрос — следующий;
- в «Уже учла» строка есть, подпись «Не знаю», ``unknown: true`` и
  ``origin: anketa`` — известным фактом это не считается;
- ``unknown`` на шаге без ``escape`` — 400, не запись;
- на multi-шаге «Не знаю» отвечает одним ключом и заменяет варианты;
- пересмотр «Не знаю» на настоящий вариант — обычный ``revise``.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import build_decision_context
from goals.models import GoalAnketaAnswer
from services.models import GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1747"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:dont-know"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]

MULTI_ESCAPE = anketa.AnketaStep(
    key="signals",
    prompt="Что беспокоит сейчас?",
    mode=anketa.MODE_MULTI,
    options=(("dry", "Сухость"), ("tired", "Усталость")),
    escape=True,
)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001747", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)]


def _install(monkeypatch, steps):
    monkeypatch.setattr(anketa, "ANKETA_STEPS", steps)
    monkeypatch.setattr(anketa, "_STEP_BY_KEY", {s.key: s for s in steps})
    monkeypatch.setattr(
        anketa, "_ANSWERABLE_KEYS", frozenset({*(s.key for s in steps), anketa.GOAL_STEP_KEY})
    )
    monkeypatch.setattr(anketa, "TOTAL_STEPS", 1 + len(steps))


@pytest.fixture
def feeling_first(monkeypatch):
    """«Не знаю» на шаге, за которым ещё есть вопрос: видно, что проход
    идёт дальше и шаг не задаётся повторно."""
    _install(monkeypatch, (FEELING, AREA))


@pytest.fixture
def multi_escape(monkeypatch):
    _install(monkeypatch, (MULTI_ESCAPE, AREA))


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


def _open(api):
    resp = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax")
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


class TestContract:
    def test_escape_step_carries_unknown_last_with_the_role(self):
        assert FEELING.escape is True
        item = anketa.as_missing_item(FEELING, answered_keys={"goal", "area"})
        last = item["options"][-1]
        assert last == {
            "key": anketa.UNKNOWN_OPTION_KEY,
            "label": anketa.UNKNOWN_OPTION_LABEL,
            "role": anketa.OPTION_ROLE_ESCAPE,
        }
        # Обычные варианты — без роли, и ровно те, что у шага.
        regular = item["options"][:-1]
        assert all("role" not in o for o in regular)
        assert [o["key"] for o in regular] == [k for k, _ in FEELING.options]

    def test_step_without_escape_is_unchanged(self):
        assert AREA.escape is False
        item = anketa.as_missing_item(AREA, answered_keys={"goal"})
        assert [o["key"] for o in item["options"]] == [k for k, _ in AREA.options]
        assert all("role" not in o for o in item["options"])

    def test_todays_steps_pass_the_guard(self):
        assert anketa.step_contract_errors(anketa.ANKETA_STEPS) == []

    def test_guard_refuses_unknown_as_a_regular_option_and_escape_on_goal(self):
        clash = anketa.AnketaStep(
            key="x", prompt="?", options=(("unknown", "Не знаю"), ("a", "A"))
        )
        assert any("зарезервированный" in e for e in anketa.step_contract_errors((clash,)))
        goal = anketa.AnketaStep(
            key=anketa.GOAL_STEP_KEY, prompt="?", options=(("a", "A"),), escape=True
        )
        assert any("выборе цели" in e for e in anketa.step_contract_errors((goal,)))

    def test_known_row_for_unknown_is_marked_and_labelled(self):
        row = anketa.as_known_answer(FEELING, option_key=anketa.UNKNOWN_OPTION_KEY, text=None)
        assert row["label"] == anketa.UNKNOWN_OPTION_LABEL
        assert row["unknown"] is True
        assert row["origin"] == anketa.ORIGIN_ANKETA
        real = anketa.as_known_answer(FEELING, option_key="calmer", text=None)
        assert real["unknown"] is False


@pytest.mark.django_db
class TestDurableUnknown:
    def test_unknown_is_stored_and_the_step_is_not_asked_again(
        self, customer, token, goal_options, feeling_first
    ):
        api = _api()
        doc = _open(api)
        assert _current(doc)["step"] == FEELING.key
        resp = _answer(api, FEELING.key, option_key=anketa.UNKNOWN_OPTION_KEY)
        assert resp.status_code == 200, resp.content
        row = GoalAnketaAnswer.objects.get(run__client=customer, step_key=FEELING.key)
        assert row.option_key == anketa.UNKNOWN_OPTION_KEY
        # Проход пошёл дальше — следующий вопрос, не тот же.
        assert _current(resp.json()["data"])["step"] == AREA.key
        known = build_decision_context(customer)["known"]["anketa"]
        assert [(r["step"], r["label"], r["unknown"]) for r in known] == [
            (FEELING.key, anketa.UNKNOWN_OPTION_LABEL, True)
        ]

    def test_unknown_on_a_step_without_escape_is_refused(
        self, customer, token, goal_options
    ):
        api = _api()
        _open(api)
        resp = _answer(api, AREA.key, option_key=anketa.UNKNOWN_OPTION_KEY)
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaAnswer.objects.filter(step_key=AREA.key).exists()
        # Положительная стража: обычный вариант на том же шаге — как прежде.
        assert _answer(api, AREA.key, option_key="face").status_code == 200

    def test_unknown_on_multi_replaces_the_array(
        self, customer, token, goal_options, multi_escape
    ):
        api = _api()
        _open(api)
        resp = _answer(api, "signals", option_key=anketa.UNKNOWN_OPTION_KEY)
        assert resp.status_code == 200, resp.content
        row = GoalAnketaAnswer.objects.get(step_key="signals")
        assert row.option_key == anketa.UNKNOWN_OPTION_KEY and row.option_keys == []

    def test_revising_unknown_to_a_real_answer(
        self, customer, token, goal_options, feeling_first
    ):
        api = _api()
        _open(api)
        assert _answer(api, FEELING.key, option_key=anketa.UNKNOWN_OPTION_KEY).status_code == 200
        resp = _answer(api, FEELING.key, option_key="calmer", revise=True)
        assert resp.status_code == 200, resp.content
        known = build_decision_context(customer)["known"]["anketa"]
        assert [(r["option_key"], r["unknown"]) for r in known] == [("calmer", False)]
