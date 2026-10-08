# flake8: noqa: F811 — фикстуры импортированы из узлов Plan Engine и приходят параметрами
"""Предложение плана и подтверждение замены (DRF-2857).

Решение владельца 08.10.2026, дословно: «Сохранённый черновик не вытесняет
действующий план без подтверждения замены». И: возможность отказаться и
оставить прежнюю версию; двойное нажатие и повтор после сбоя — без дублей.

Что держат узлы:

* при действующем плане цели новый сохраняется ПРЕДЛОЖЕНИЕМ и ничего не гасит;
* действующим предложение делает только команда замены, которая называет
  именно заменяемый план; подтверждение, отнесённое к другому плану, — отказ;
* от предложения можно отказаться — действующий план остаётся как был;
* предложение не действует: его шаги не исполняются;
* то же решение не даёт второго плана, каким бы ни было подтверждение.
"""
from __future__ import annotations

import copy
import uuid

import pytest

from wellness.models import PersonalPlan, Plan, PlanRevision
from wellness.tests.test_plan_engine_2857 import (  # noqa: F401 — фикстуры того же сценария
    LITE_BODY,
    LITE_URL,
    OWNER,
    PLAN_URL,
    STATE_URL,
    STRANGER,
    _api,
    _command,
    _step,
    _token_and_flags,
    goal,
    owner,
    stranger,
)

pytestmark = pytest.mark.django_db

REPLACE_URL = "/api/v1/internal/me/plan/replace/"
RESOLUTION_URL = "/api/v1/internal/me/plan/steps/resolution/"
SAFETY = {"safety_state": "NORMAL", "safety_policy_version": "pre_check-test", "evaluated_at_revision": 4}


def _post(body: dict):
    return _api().post(PLAN_URL, body, format="json")


def _saved(goal, **over) -> dict:
    resp = _post(_command(goal, **over))
    assert resp.status_code == 201, resp.content
    return resp.json()["data"]


def _replace(plan_id: str, replaces: str, who: str = OWNER, **over):
    return _api(who).post(
        REPLACE_URL, {"plan_id": plan_id, "replaces_plan_id": replaces, **SAFETY, **over}, format="json",
    )


def _read() -> dict:
    return _api().get(PLAN_URL).json()["data"]


class TestTheFirstPlanOfAGoalIsInEffectAtOnce:
    def test_with_no_plan_in_effect_the_saved_plan_is_active(self, goal) -> None:
        data = _saved(goal)
        assert data["plan"]["status"] == "active"
        assert "replaces" not in data
        assert _read() == {"plan": data["plan"], "proposal": None}

    def test_a_paused_plan_is_not_a_plan_in_effect(self, goal) -> None:
        """Как и прежде: рядом с приостановленным новый план сохраняется действующим."""
        first = _saved(goal)["plan"]["plan_id"]
        assert _api().post(STATE_URL, {"plan_id": first, "state": "paused"}, format="json").status_code == 200
        data = _saved(goal)
        assert (data["plan"]["status"], "replaces" in data) == ("active", False)


class TestASavedProposalDoesNotDisplaceThePlanInEffect:
    def test_the_new_plan_is_saved_as_a_proposal_and_nothing_is_touched(self, goal) -> None:
        active = _saved(goal)["plan"]
        data = _saved(goal, steps=[_step("n1")])
        assert data["plan"]["status"] == "proposed"
        assert data["plan"]["in_effect"] is False
        assert data["replaces"] == {"plan_id": active["plan_id"]}
        current = _read()
        assert current["plan"] == active  # действующий — ровно тот же, ни одно поле не изменилось
        assert current["proposal"]["plan_id"] == data["plan"]["plan_id"]
        assert sorted(Plan.objects.values_list("status", flat=True)) == ["active", "proposed"]

    def test_status_and_the_replacement_hint_always_agree(self, goal) -> None:
        for data in (_saved(goal), _saved(goal), _saved(goal)):
            assert (data["plan"]["status"] == "proposed") is ("replaces" in data)

    def test_a_newer_proposal_supersedes_the_older_one_not_the_plan(self, goal) -> None:
        active = _saved(goal)["plan"]["plan_id"]
        older = _saved(goal)["plan"]["plan_id"]
        newer = _saved(goal)["plan"]["plan_id"]
        statuses = dict(Plan.objects.values_list("pk", "status"))
        assert {str(k): v for k, v in statuses.items()} == {
            active: "active", older: "superseded", newer: "proposed",
        }

    def test_the_steps_of_a_proposal_are_not_executable(self, goal) -> None:
        _saved(goal)
        proposal = _saved(goal)["plan"]["plan_id"]
        resp = _api().post(
            RESOLUTION_URL,
            {"plan_id": proposal, "step_id": "s1", "level": "SERVICE",
             "canonical_service_ref": str(uuid.uuid4()), "resolver_decision_id": "search", **SAFETY},
            format="json",
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["details"]["reason"] == "plan_not_in_effect"

    def test_saving_a_proposal_leaves_the_lite_plan_alone(self, goal, settings) -> None:
        """Вытеснение Lite — когда план СТАНОВИТСЯ действующим, не раньше."""
        _saved(goal)
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        _saved(goal)  # предложение
        assert PersonalPlan.objects.get().status == "active"


class TestReplacingNeedsAConfirmationOfThatVeryPlan:
    @pytest.fixture
    def pair(self, goal) -> tuple[str, str]:
        active = _saved(goal)["plan"]["plan_id"]
        return active, _saved(goal, steps=[_step("n1")])["plan"]["plan_id"]

    def test_the_proposal_becomes_the_plan_and_the_old_one_is_history(self, pair) -> None:
        active, proposal = pair
        resp = _replace(proposal, active)
        assert resp.status_code == 200, resp.content
        data = resp.json()["data"]
        assert (data["replaced"], data["plan"]["status"], data["plan"]["in_effect"]) == (True, "active", True)
        assert Plan.objects.get(pk=active).status == "superseded"
        assert _read()["plan"]["plan_id"] == proposal and _read()["proposal"] is None
        # История цела: прежний план и его ревизия на месте.
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (2, 2)

    def test_a_second_tap_changes_nothing(self, pair) -> None:
        active, proposal = pair
        first, again = _replace(proposal, active), _replace(proposal, active)
        assert (first.status_code, again.status_code) == (200, 200)
        assert (first.json()["data"]["replaced"], again.json()["data"]["replaced"]) == (True, False)
        assert sorted(Plan.objects.values_list("status", flat=True)) == ["active", "superseded"]

    def test_a_confirmation_given_about_another_plan_is_refused(self, pair, goal) -> None:
        """Человек сказал «да» про план А, а действует уже Б — замена не выполняется."""
        active, proposal = pair
        assert _api().post(STATE_URL, {"plan_id": active, "state": "archived"}, format="json").status_code == 200
        other = _saved(goal, steps=[_step("o1")])["plan"]["plan_id"]  # действующего нет → сразу действует
        resp = _replace(proposal, active)
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_REPLACEMENT_TARGET_CHANGED"
        assert resp.json()["error"]["details"] == {"current_plan_id": other}
        assert Plan.objects.get(pk=other).status == "active"
        assert Plan.objects.get(pk=proposal).status == "proposed"

    def test_when_nothing_is_in_effect_any_more_the_target_has_changed_too(self, pair) -> None:
        active, proposal = pair
        _api().post(STATE_URL, {"plan_id": active, "state": "archived"}, format="json")
        resp = _replace(proposal, active)
        assert resp.json()["error"]["code"] == "PLAN_REPLACEMENT_TARGET_CHANGED"
        assert resp.json()["error"]["details"] == {"current_plan_id": None}

    def test_an_old_card_of_a_superseded_proposal_gets_a_named_refusal(self, goal) -> None:
        active = _saved(goal)["plan"]["plan_id"]
        older = _saved(goal)["plan"]["plan_id"]
        _saved(goal)
        resp = _replace(older, active)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_TRANSITION_REFUSED"
        assert resp.json()["error"]["details"] == {"from": "superseded", "to": "active"}

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_a_blocking_verdict_does_not_replace(self, pair, state) -> None:
        active, proposal = pair
        resp = _replace(proposal, active, safety_state=state)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_SAVE_SAFETY_BLOCKED"
        assert sorted(Plan.objects.values_list("status", flat=True)) == ["active", "proposed"]

    def test_a_repeat_of_a_done_replacement_is_not_judged_again_under_stop(self, pair) -> None:
        active, proposal = pair
        _replace(proposal, active)
        assert _replace(proposal, active, safety_state="STOP").status_code == 200

    def test_a_strangers_plans_are_not_found(self, pair, stranger) -> None:
        active, proposal = pair
        resp = _replace(proposal, active, who=STRANGER)
        assert (resp.status_code, resp.json()["error"]["details"]["reason"]) == (404, "plan_not_found")

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"plan_id": "nope"}, "plan_id_malformed"),
            ({"replaces_plan_id": ""}, "replaces_plan_id_malformed"),
            ({"safety_state": "NOT_APPLICABLE"}, "safety_state_invalid"),
            ({"evaluated_at_revision": "4"}, "safety_revision_malformed"),
        ],
    )
    def test_malformed_requests_by_name(self, pair, over, reason) -> None:
        active, proposal = pair
        body = {"plan_id": proposal, "replaces_plan_id": active, **SAFETY, **over}
        resp = _api().post(REPLACE_URL, body, format="json")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason

    def test_replacing_displaces_the_lite_plan_outside_test_data(self, pair, settings) -> None:
        active, proposal = pair
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        assert _replace(proposal, active).status_code == 200
        assert PersonalPlan.objects.get().status == "superseded"

    def test_engine_off_404(self, pair, settings) -> None:
        active, proposal = pair
        settings.PLAN_ENGINE_ENABLED = False
        assert _replace(proposal, active).json()["error"]["code"] == "PLAN_ENGINE_DISABLED"


class TestRefusingAProposalKeepsThePlan:
    def test_archiving_the_proposal_leaves_the_plan_in_effect_untouched(self, goal) -> None:
        active = _saved(goal)["plan"]
        proposal = _saved(goal)["plan"]["plan_id"]
        resp = _api().post(STATE_URL, {"plan_id": proposal, "state": "archived"}, format="json")
        assert resp.status_code == 200, resp.content
        assert _read() == {"plan": active, "proposal": None}

    @pytest.mark.parametrize("state", ["active", "paused"])
    def test_a_proposal_cannot_be_moved_by_a_status_change(self, goal, state) -> None:
        """Действующим предложение делает замена с подтверждением, не смена статуса."""
        _saved(goal)
        proposal = _saved(goal)["plan"]["plan_id"]
        resp = _api().post(STATE_URL, {"plan_id": proposal, "state": state}, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_TRANSITION_REFUSED"
        assert Plan.objects.get(pk=proposal).status == "proposed"


class TestOneDecisionOnePlan:
    """Двойное нажатие и повтор после сбоя — без дублей, и не только пока бот
    правильно хранит ревизию показа."""

    def test_another_confirmation_of_the_same_decision_is_the_same_plan(self, goal) -> None:
        body = _command(goal)
        again = copy.deepcopy(body)
        again["confirmation"]["state_revision"] = 8
        first, second = _post(body), _post(again)
        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["data"]["created"] is False
        assert second.json()["data"]["plan"]["plan_id"] == first.json()["data"]["plan"]["plan_id"]
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)

    def test_the_same_holds_for_a_proposal(self, goal) -> None:
        _saved(goal)
        body = _command(goal, steps=[_step("n1")])
        again = copy.deepcopy(body)
        again["confirmation"]["state_revision"] = 9
        first, second = _post(body), _post(again)
        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["data"]["plan"]["status"] == "proposed"
        assert "replaces" in second.json()["data"]
        assert Plan.objects.count() == 2

    def test_the_same_decision_with_other_content_is_a_conflict(self, goal) -> None:
        body = _command(goal)
        other = copy.deepcopy(body)
        other["confirmation"]["state_revision"] = 8
        other["decision"]["steps"] = [_step("s9")]
        other["decision"]["validation"]["step_validations"] = {"s9": "INCOMPLETE"}
        assert _post(body).status_code == 201
        resp = _post(other)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_IDEMPOTENCY_CONFLICT"
        assert Plan.objects.count() == 1

    def test_a_decision_whose_plan_was_archived_can_be_saved_again(self, goal) -> None:
        """Рубеж — про действующий план и предложение; от архива не защищает и не должен."""
        body = _command(goal)
        first = _post(body).json()["data"]["plan"]["plan_id"]
        _api().post(STATE_URL, {"plan_id": first, "state": "archived"}, format="json")
        again = copy.deepcopy(body)
        again["confirmation"]["state_revision"] = 8
        resp = _post(again)
        assert resp.status_code == 201
        assert resp.json()["data"]["plan"]["plan_id"] != first
