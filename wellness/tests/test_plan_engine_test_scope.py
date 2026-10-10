"""Вторая линия изоляции синтетического прогона: кому включён Plan Engine.

Решение владельца 10.10.2026: пока на стенде включены тестовые данные
(``SYNTHETIC_TEST_DATA_ENABLED``), ручки Плана отвечают «выключено» всем, кроме
субъектов серверного списка. Линия в каталоге и от бота не зависит.

Что держат узлы:

* правило по умолчанию отказывает: пустой список, не тот человек, сбой
  проверки — «выключено»;
* доступ к Плану — по списку; признак тестовой персоны его не даёт и не
  отнимает (он — у замка синтетических данных);
* не-тестовый субъект получает на КАЖДОЙ ручке Плана ровно тот же ответ, что
  при выключенном движке, — и ничего не пишет;
* субъект списка под теми же флагами работает как прежде;
* без флага тестовых данных правило не действует вовсе.
"""
from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from wellness.models import Plan
from wellness.plan_engine import plan_engine_enabled_for
from wellness.plan_restrictions import CAUSES, Cause
from wellness.tests.test_plan_compose_2871 import _api
from wellness.tests.test_plan_gate import (  # noqa: F401 — фикстуры того же сценария
    LABELS_URL,
    URLS,
    _bodies,
    _token_and_flag,
    _writes,
    goal,
    owner,
)
from wellness.tests.test_plan_engine_2857 import PLAN_URL, _command, _save
from wellness.tests.test_plan_engine_steps_2868 import SAFETY

pytestmark = pytest.mark.django_db

CANDIDATES_URL = "/api/v1/internal/me/plan/steps/candidates/"
#: Все пишущие и обрабатывающие ручки Плана: перечень гейта + кандидаты + подписи.
EVERY_POST = {**URLS, "candidates": CANDIDATES_URL, "labels": LABELS_URL}
CAUSE = "TEST_PLAN_QUESTION"


def _body(endpoint: str, goal, saved) -> dict:  # noqa: F811
    """Тело по форме на каждую ручку: разбор тела идёт раньше проверки «кому
    включён движок», и пустое тело отвечало бы 400 при любых флагах — узел
    сравнивал бы два одинаковых отказа по чужой причине."""
    plan = {"plan_id": str(saved.pk)}
    return {
        "save": _command(goal),
        "state": {**plan, "state": Plan.Status.PAUSED},
        "decision": _bodies(goal, saved)["decision"],
        "labels": {"keys": ["cap:relaxation-massage"]},
        "candidates": {**plan, "step_id": "s1", **SAFETY},
        "resolution": {
            **plan, "step_id": "s1", "level": "SERVICE", "canonical_service_ref": str(uuid.uuid4()),
            "resolver_decision_id": "search", **SAFETY,
        },
        "booking": {**plan, "step_id": "s1", "appointment_id": str(uuid.uuid4()), **SAFETY},
        "restriction": {**plan, "scope": "PLAN", "cause": CAUSE, "question_id": "plan.question", **SAFETY},
        "lift": {
            **plan, "restriction_id": str(uuid.uuid4()), "lift_kind": "answered", "answer_option_id": "opt-no",
            **SAFETY,
        },
        "replace": {**plan, "replaces_plan_id": str(uuid.uuid4()), **SAFETY},
    }[endpoint]


def _make_test_subject(user, settings) -> None:
    """В список — и только: признак тестовой персоны для доступа к Плану не нужен."""
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(user.pk)]


@pytest.fixture
def test_data_on(settings):
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = []


class TestTheRule:
    @pytest.mark.parametrize(
        "engine, test_data, listed, persona, enabled",
        [
            (False, False, False, False, False),
            (False, True, True, True, False),  # движок выключен — никакой список его не включает
            (True, False, False, False, True),  # тестовых данных нет — правило не действует
            (True, True, True, True, True),  # в списке, тестовая персона
            (True, True, True, False, True),  # в списке без признака персоны — доступ по списку
            (True, True, False, True, False),  # персона, но не в списке
            (True, True, False, False, False),  # обычный человек
        ],
    )
    def test_who_the_engine_is_on_for(
        self, owner, settings, engine, test_data, listed, persona, enabled,  # noqa: F811
    ) -> None:
        settings.PLAN_ENGINE_ENABLED = engine
        settings.SYNTHETIC_TEST_DATA_ENABLED = test_data
        settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(owner.pk)] if listed else ["00000000-0000-0000-0000-000000000000"]
        type(owner).objects.filter(pk=owner.pk).update(is_test_persona=persona)
        owner.refresh_from_db()
        assert plan_engine_enabled_for(owner) is enabled

    def test_an_empty_list_lets_nobody_in(self, owner, settings, test_data_on) -> None:  # noqa: F811
        type(owner).objects.filter(pk=owner.pk).update(is_test_persona=True)
        owner.refresh_from_db()
        assert plan_engine_enabled_for(owner) is False

    @pytest.mark.parametrize("nobody", [None, object()])
    def test_nobody_and_an_unknown_caller_are_refused(self, settings, test_data_on, nobody) -> None:
        assert plan_engine_enabled_for(nobody) is False

    def test_a_failing_check_closes_the_engine(self, owner, settings, test_data_on) -> None:  # noqa: F811
        _make_test_subject(owner, settings)
        owner.refresh_from_db()
        assert plan_engine_enabled_for(owner) is True

        settings.SYNTHETIC_TEST_SUBJECT_IDS = 5  # непригодное значение: перебрать нельзя
        assert plan_engine_enabled_for(owner) is False

    def test_the_list_opens_the_plan_but_not_the_synthetic_data(
        self, owner, settings, test_data_on,  # noqa: F811
    ) -> None:
        """Два замка разведены: по списку — План; синтетика — ещё и тестовая персона."""
        from services.synthetic import grant_for

        _make_test_subject(owner, settings)
        owner.refresh_from_db()
        assert plan_engine_enabled_for(owner) is True
        assert grant_for(owner) is None


class TestAPersonOutsideTheListGetsADisabledEngine:
    """Не «отказ тестового контура», а ровно ответ выключенного движка."""

    @pytest.mark.parametrize("endpoint", sorted(EVERY_POST))
    def test_every_endpoint_answers_exactly_as_with_the_engine_off(
        self, owner, goal, settings, monkeypatch, endpoint,  # noqa: F811
    ) -> None:
        # Таблица причин каталога пуста до решения владельца — причина подставлена узлом.
        monkeypatch.setitem(CAUSES, CAUSE, Cause(scopes=frozenset({"PLAN"}), lift_kinds=frozenset()))
        saved = _save(goal)
        body = _body(endpoint, goal, saved)

        settings.PLAN_ENGINE_ENABLED = False
        off = _api().post(EVERY_POST[endpoint], body, format="json")

        settings.PLAN_ENGINE_ENABLED = True
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = ["00000000-0000-0000-0000-000000000000"]
        before = Plan.objects.filter(pk=saved.pk).values().get()
        with CaptureQueriesContext(connection) as queries:
            scoped_out = _api().post(EVERY_POST[endpoint], body, format="json")

        assert off.status_code == 404, (endpoint, off.content[:300])
        assert off.json()["error"]["code"] == "PLAN_ENGINE_DISABLED", endpoint
        assert (scoped_out.status_code, scoped_out.json()) == (off.status_code, off.json()), endpoint
        assert _writes(queries) == [], endpoint
        assert Plan.objects.filter(pk=saved.pk).values().get() == before

    def test_reading_the_plan_answers_as_with_the_engine_off(self, owner, goal, settings) -> None:  # noqa: F811
        _save(goal)
        settings.PLAN_ENGINE_ENABLED = False
        off = _api().get(PLAN_URL)
        settings.PLAN_ENGINE_ENABLED = True
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        scoped_out = _api().get(PLAN_URL)
        assert off.status_code == 200 and off.json()["data"]["plan"] is None
        assert scoped_out.json() == off.json()

    def test_a_new_plan_is_not_saved(self, owner, goal, settings, test_data_on) -> None:  # noqa: F811
        response = _api().post(PLAN_URL, _command(goal), format="json")
        assert response.status_code == 404, response.content[:300]
        assert Plan.objects.count() == 0


class TestATestSubjectWorksAsBefore:
    def test_the_subject_saves_and_reads_a_plan_under_both_flags(
        self, owner, goal, settings, test_data_on,  # noqa: F811
    ) -> None:
        _make_test_subject(owner, settings)
        saved = _api().post(PLAN_URL, _command(goal), format="json")
        assert saved.status_code == 201, saved.content[:300]
        assert _api().get(PLAN_URL).json()["data"]["plan"]["plan_id"] == saved.json()["data"]["plan"]["plan_id"]

    def test_taken_off_the_list_the_subject_loses_the_engine_and_keeps_the_plan(
        self, owner, goal, settings, test_data_on,  # noqa: F811
    ) -> None:
        _make_test_subject(owner, settings)
        assert _api().post(PLAN_URL, _command(goal), format="json").status_code == 201
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []
        assert _api().get(PLAN_URL).json()["data"]["plan"] is None
        assert _api().post(PLAN_URL, _command(goal), format="json").status_code == 404
        assert Plan.objects.count() == 1


class TestWithoutTestDataTheRuleIsNotThere:
    def test_an_ordinary_person_saves_a_plan_when_only_the_engine_is_on(
        self, owner, goal, settings,  # noqa: F811
    ) -> None:
        settings.SYNTHETIC_TEST_DATA_ENABLED = False
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []
        assert _api().post(PLAN_URL, _command(goal), format="json").status_code == 201
