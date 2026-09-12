"""Типы ответа по смыслу — контракт ``mode`` (DRF-1746, макет C03 P11).

Режим — свойство шага, едет в ``missing`` данными; экран рисует по нему
компонент. Что заперто:

- у каждого элемента ``missing`` есть ``mode``; сегодняшние шаги —
  ``single`` (содержание анкеты этим срезом не меняется);
- ``scale`` несёт подписи концов, ``text`` — лимит; у ``single`` нет ни
  того, ни другого (положительная стража: старая форма не разрослась);
- ``multi`` отвечает ТОЛЬКО массивом ``option_keys`` одним запросом:
  одиночный ключ на multi-шаге и массив на single-шаге — 400, не запись;
  ключи хранятся в порядке вариантов шага, не тапов; строка «Уже учла»
  одна на шаг, подписи через запятую;
- ``text`` длиннее лимита — 400;
- сторож формы шага ловит режим без нужных входов.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import build_decision_context
from goals.models import GoalAnketaAnswer
from services.models import GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1746"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:answer-modes"

MULTI = anketa.AnketaStep(
    key="signals",
    prompt="Что беспокоит сейчас?",
    mode=anketa.MODE_MULTI,
    options=(
        ("dry", "Сухость"),
        ("tired", "Усталость"),
        ("tension", "Напряжение"),
        ("none", "Ничего из этого"),
    ),
)
SCALE = anketa.AnketaStep(
    key="intensity",
    prompt="Насколько это мешает?",
    mode=anketa.MODE_SCALE,
    options=(("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5")),
    scale_ends=("Почти нет", "Очень"),
)
TEXT = anketa.AnketaStep(
    key="note",
    prompt="Одним словом — что хочется?",
    mode=anketa.MODE_TEXT,
    allow_free_text=True,
)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001746", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)]


@pytest.fixture
def mixed_steps(monkeypatch):
    """Проход с шагами всех режимов. Порядок — единственный источник правды
    о последовательности, и он в ``ANKETA_STEPS``; подменяются все три
    производные от него разом, иначе шаг был бы «известен» одной функции
    и «неизвестен» другой."""
    steps = (MULTI, SCALE, TEXT)
    monkeypatch.setattr(anketa, "ANKETA_STEPS", steps)
    monkeypatch.setattr(anketa, "_STEP_BY_KEY", {s.key: s for s in steps})
    monkeypatch.setattr(
        anketa, "_ANSWERABLE_KEYS", frozenset({*(s.key for s in steps), anketa.GOAL_STEP_KEY})
    )
    monkeypatch.setattr(anketa, "TOTAL_STEPS", 1 + len(steps))
    return steps


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


# --------------------------------------------------------------------------- #
# Контракт документа                                                          #
# --------------------------------------------------------------------------- #
class TestContract:
    def test_todays_steps_are_single_and_pass_the_guard(self):
        assert anketa.step_contract_errors(anketa.ANKETA_STEPS) == []
        assert {s.mode for s in anketa.ANKETA_STEPS} == {anketa.MODE_SINGLE}

    def test_single_item_carries_mode_and_nothing_scale_or_text(self):
        item = anketa.as_missing_item(anketa.ANKETA_STEPS[0], answered_keys={"goal"})
        assert item["mode"] == anketa.MODE_SINGLE
        assert "scale" not in item
        assert "text_limit" not in item

    def test_scale_item_carries_the_ends_in_option_order(self):
        item = anketa.as_missing_item(SCALE, answered_keys={"goal"})
        assert item["mode"] == anketa.MODE_SCALE
        assert item["scale"] == {"low_label": "Почти нет", "high_label": "Очень"}
        assert [o["key"] for o in item["options"]] == ["1", "2", "3", "4", "5"]

    def test_text_item_carries_the_limit(self):
        item = anketa.as_missing_item(TEXT, answered_keys={"goal"})
        assert item["mode"] == anketa.MODE_TEXT
        assert item["text_limit"] == anketa.TEXT_ANSWER_LIMIT
        assert item["allow_free_text"] is True
        assert item["options"] == []

    def test_multi_item_is_marked_multi(self):
        item = anketa.as_missing_item(MULTI, answered_keys={"goal"})
        assert item["mode"] == anketa.MODE_MULTI
        assert len(item["options"]) == 4

    @pytest.mark.parametrize(
        "step, fragment",
        [
            (anketa.AnketaStep(key="x", prompt="?", mode="fancy"), "неизвестный mode"),
            (
                anketa.AnketaStep(
                    key="x", prompt="?", mode=anketa.MODE_TEXT, options=(("a", "A"),),
                    allow_free_text=True,
                ),
                "text",
            ),
            (anketa.AnketaStep(key="x", prompt="?", mode=anketa.MODE_TEXT), "text"),
            (
                anketa.AnketaStep(
                    key="x", prompt="?", mode=anketa.MODE_SCALE,
                    options=(("1", "1"), ("2", "2")),
                ),
                "scale",
            ),
            (
                anketa.AnketaStep(
                    key="x", prompt="?", mode=anketa.MODE_SINGLE,
                    options=(("1", "1"),), scale_ends=("a", "b"),
                ),
                "только у scale",
            ),
            (anketa.AnketaStep(key="x", prompt="?", mode=anketa.MODE_MULTI), "multi"),
        ],
    )
    def test_guard_names_a_step_whose_shape_does_not_fit_its_mode(self, step, fragment):
        errors = anketa.step_contract_errors((step,))
        assert errors and fragment in errors[0], errors

    def test_known_answer_for_multi_joins_labels_in_step_order(self):
        row = anketa.as_known_answer(
            MULTI, option_key=None, text=None, option_keys=["tension", "dry"]
        )
        assert row["label"] == "Сухость, Напряжение"
        assert row["option_keys"] == ["tension", "dry"]
        assert row["mode"] == anketa.MODE_MULTI


# --------------------------------------------------------------------------- #
# API: форма ответа под режим                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
class TestAnswerShape:
    def _open(self, api):
        resp = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax")
        assert resp.status_code == 200, resp.content
        return resp

    def test_multi_takes_an_array_and_stores_it_in_step_order(
        self, customer, token, goal_options, mixed_steps
    ):
        api = _api()
        self._open(api)
        resp = _answer(api, "signals", option_keys=["tension", "dry"])
        assert resp.status_code == 200, resp.content
        row = GoalAnketaAnswer.objects.get(run__client=customer, step_key="signals")
        assert row.option_keys == ["dry", "tension"]
        assert row.option_key is None and row.answer_text is None
        doc = build_decision_context(customer)
        assert [a["label"] for a in doc["known"]["anketa"]] == ["Сухость, Напряжение"]
        # Проход сдвинулся ровно на один шаг.
        assert _current(resp.json()["data"])["step"] == "intensity"

    def test_multi_refuses_a_single_key(self, customer, token, goal_options, mixed_steps):
        api = _api()
        self._open(api)
        resp = _answer(api, "signals", option_key="dry")
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaAnswer.objects.filter(step_key="signals").exists()

    def test_multi_refuses_an_unknown_key(self, customer, token, goal_options, mixed_steps):
        api = _api()
        self._open(api)
        resp = _answer(api, "signals", option_keys=["dry", "nope"])
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaAnswer.objects.filter(step_key="signals").exists()

    def test_single_refuses_an_array(self, customer, token, goal_options):
        """Сегодняшний шаг ``area`` — single: массив на нём не запись."""
        api = _api()
        self._open(api)
        resp = _answer(api, "area", option_keys=["face"])
        assert resp.status_code == 400, resp.content
        assert not GoalAnketaAnswer.objects.filter(step_key="area").exists()
        # Положительная стража: одиночный ключ на том же шаге — как прежде.
        assert _answer(api, "area", option_key="face").status_code == 200

    def test_text_refuses_more_than_the_limit(self, customer, token, goal_options, mixed_steps):
        api = _api()
        self._open(api)
        assert _answer(api, "signals", option_keys=["none"]).status_code == 200
        assert _answer(api, "intensity", option_key="3").status_code == 200
        too_long = "х" * (anketa.TEXT_ANSWER_LIMIT + 1)
        assert _answer(api, "note", text=too_long).status_code == 400
        resp = _answer(api, "note", text="х" * anketa.TEXT_ANSWER_LIMIT)
        assert resp.status_code == 200, resp.content
        assert GoalAnketaAnswer.objects.get(step_key="note").answer_text == (
            "х" * anketa.TEXT_ANSWER_LIMIT
        )

    def test_revise_multi_replaces_the_array(self, customer, token, goal_options, mixed_steps):
        api = _api()
        self._open(api)
        assert _answer(api, "signals", option_keys=["dry"]).status_code == 200
        resp = _answer(api, "signals", option_keys=["none"], revise=True)
        assert resp.status_code == 200, resp.content
        assert GoalAnketaAnswer.objects.get(step_key="signals").option_keys == ["none"]
