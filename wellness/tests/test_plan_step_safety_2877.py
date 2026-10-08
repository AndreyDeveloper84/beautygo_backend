"""Безопасность хода на каждом действии с шагом (DRF-2877, Plan WP3 §2).

Контракт PLAN_ENGINE_CONTRACT v1.0 §6.1 («``safety_state`` на входе шага
обязателен»), §9.3, §12. Дыра, которую закрывает лист: план сохранён при
``NORMAL``, позже человек написал тревожное — а шаг всё ещё вёл к услуге и к
записи, потому что действия с шагом безопасность не спрашивали.

Что держат узлы, на ОБЕИХ ручках шага:

* ``STOP`` и ``UNKNOWN`` — отказ 409 ``safety_blocked``, ни одной строки;
* отказ не меняет ни статус плана, ни цель: гейт отказывает действию;
* молчание о безопасности — отказ, а не «норма»; ``NOT_APPLICABLE`` — отказ;
* при допустимом состоянии оно, версия политики и ревизия сохраняются в строке;
* отказ по безопасности не раскрывает, существует ли план (он раньше поиска).

Сценарий записи создаётся настоящей ручкой ``POST /internal/appointments/``.
"""
# flake8: noqa: F811 — фикстуры импортированы из узлов WP2 и приходят параметрами
from __future__ import annotations

import pytest

from goals.models import ClientGoal
from wellness.models import Plan, PlanStepBooking, PlanStepResolution
from wellness.plan_safety import SAFETY_BLOCKING, SAFETY_STATES, SafetyInputError, parse_safety_input
from wellness.tests.test_plan_engine_steps_2868 import (  # noqa: F401 — фикстуры того же сценария
    BOOKING_URL,
    DECISION,
    OWNER,
    RESOLUTION_URL,
    SAFETY,
    STRANGER,
    _api,
    _book,
    _link,
    _resolve,
    _save,
    _to_offer,
    _token_and_flag,
    canon,
    category,
    goal,
    offer,
    owner,
    specialist,
    stranger,
    tenant,
)

pytestmark = pytest.mark.django_db

BLOCKING = ["STOP", "UNKNOWN"]
PASSING = ["NORMAL", "CAUTION"]
#: DRF-2877 — «уточнить»: не запрет, а «сначала вопрос»; действие с шагом ждёт.
PENDING = ["CLARIFY"]


def _safety(state: str) -> dict:
    return {"safety_state": state, "safety_policy_version": "pre_check-abc", "evaluated_at_revision": 9}


def _offer_body(canon, offer) -> dict:
    return {"level": "OFFER", "canonical_service_ref": str(canon.id), "tenant_offer_ref": str(offer.id)}


def _snapshot():
    return (list(Plan.objects.values()), list(ClientGoal.objects.values()))


class TestTheVocabulary:
    def test_the_two_sets_are_the_contracts(self) -> None:
        assert SAFETY_STATES == {"NORMAL", "CLARIFY", "CAUTION", "STOP", "UNKNOWN"}
        assert SAFETY_BLOCKING == set(BLOCKING)
        assert set(PASSING) | set(PENDING) | set(BLOCKING) == SAFETY_STATES

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            ({}, "safety_state_invalid"),
            (None, "safety_state_invalid"),
            ({**_safety("NORMAL"), "safety_state": "NOT_APPLICABLE"}, "safety_state_invalid"),
            ({**_safety("NORMAL"), "safety_state": "normal"}, "safety_state_invalid"),
            ({**_safety("NORMAL"), "safety_policy_version": ""}, "safety_policy_version_missing"),
            ({"safety_state": "NORMAL", "evaluated_at_revision": 1}, "safety_policy_version_missing"),
            ({"safety_state": "NORMAL", "safety_policy_version": "v"}, "safety_revision_malformed"),
            ({**_safety("NORMAL"), "evaluated_at_revision": -1}, "safety_revision_malformed"),
            ({**_safety("NORMAL"), "evaluated_at_revision": True}, "safety_revision_malformed"),
            ({**_safety("NORMAL"), "evaluated_at_revision": "3"}, "safety_revision_malformed"),
        ],
    )
    def test_each_malformed_input_is_refused_by_name(self, body, reason) -> None:
        with pytest.raises(SafetyInputError) as exc:
            parse_safety_input(body)
        assert exc.value.reason == reason


class TestResolution:
    @pytest.mark.parametrize("state", BLOCKING)
    def test_blocking_safety_refuses_and_writes_nothing(self, goal, canon, offer, state) -> None:
        plan = _save(goal)
        before = _snapshot()
        resp = _resolve(plan, **_offer_body(canon, offer), **_safety(state))
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_NOT_EXECUTABLE"
        assert resp.json()["error"]["details"]["reason"] == "safety_blocked"
        assert not PlanStepResolution.objects.exists()
        # Гейт отказывает действию, план и цель не трогает (§4.5).
        assert _snapshot() == before

    def test_clarify_waits_for_the_question_under_its_own_name(self, goal, canon, offer) -> None:
        plan = _save(goal)
        resp = _resolve(plan, **_offer_body(canon, offer), **_safety("CLARIFY"))
        assert resp.status_code == 409
        assert resp.json()["error"]["details"] == {"reason": "clarify_pending"}
        assert not PlanStepResolution.objects.exists()

    @pytest.mark.parametrize("state", PASSING)
    def test_non_blocking_safety_passes_and_is_recorded(self, goal, canon, offer, state) -> None:
        plan = _save(goal)
        resp = _resolve(plan, **_offer_body(canon, offer), **_safety(state))
        assert resp.status_code == 201, resp.content
        row = PlanStepResolution.objects.get()
        assert (row.safety_state, row.safety_policy_version, row.safety_evaluated_at_revision) == (
            state, "pre_check-abc", 9,
        )

    @pytest.mark.parametrize("missing", ["safety_state", "safety_policy_version", "evaluated_at_revision"])
    def test_silence_about_safety_is_a_refusal_not_normal(self, goal, canon, offer, missing) -> None:
        plan = _save(goal)
        body = {"plan_id": str(plan.id), "step_id": "s1", "resolver_decision_id": DECISION,
                **_offer_body(canon, offer), **_safety("NORMAL")}
        del body[missing]
        resp = _api().post(RESOLUTION_URL, body, format="json")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["code"] == "PLAN_CONTRACT_VIOLATION"
        assert not PlanStepResolution.objects.exists()

    def test_not_applicable_is_refused_not_read_as_cleared(self, goal, canon, offer) -> None:
        plan = _save(goal)
        resp = _resolve(plan, **_offer_body(canon, offer), **_safety("NOT_APPLICABLE"))
        assert resp.status_code == 400
        assert resp.json()["error"]["details"]["reason"] == "safety_state_invalid"
        assert not PlanStepResolution.objects.exists()

    def test_the_safety_refusal_comes_before_the_plan_lookup(self, goal, canon, offer, stranger) -> None:
        """По коду отказа нельзя узнать, существует ли чужой план."""
        plan = _save(goal)
        theirs = _resolve(plan, who=STRANGER, **_offer_body(canon, offer), **_safety("STOP"))
        assert theirs.status_code == 409
        assert theirs.json()["error"]["details"]["reason"] == "safety_blocked"

    def test_a_repeat_under_stop_is_refused_even_though_the_row_exists(self, goal, canon, offer) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        again = _resolve(plan, **_offer_body(canon, offer), **_safety("STOP"))
        assert again.status_code == 409
        assert PlanStepResolution.objects.get().safety_state == "NORMAL"


class TestBookingLink:
    @pytest.fixture
    def ready(self, goal, canon, offer, specialist, owner):
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        return plan, _book(owner, specialist, offer)

    @pytest.mark.parametrize("state", BLOCKING)
    def test_blocking_safety_refuses_and_writes_nothing(self, ready, state) -> None:
        plan, appt = ready
        before = _snapshot()
        resp = _link(plan, appt, **_safety(state))
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["details"]["reason"] == "safety_blocked"
        assert not PlanStepBooking.objects.exists()
        assert _snapshot() == before

    @pytest.mark.parametrize("state", PASSING)
    def test_non_blocking_safety_passes_and_is_recorded(self, ready, state) -> None:
        plan, appt = ready
        resp = _link(plan, appt, **_safety(state))
        assert resp.status_code == 201, resp.content
        link = PlanStepBooking.objects.get()
        assert (link.safety_state, link.safety_policy_version, link.safety_evaluated_at_revision) == (
            state, "pre_check-abc", 9,
        )

    @pytest.mark.parametrize("missing", ["safety_state", "safety_policy_version", "evaluated_at_revision"])
    def test_silence_about_safety_is_a_refusal_not_normal(self, ready, missing) -> None:
        plan, appt = ready
        body = {"plan_id": str(plan.id), "step_id": "s1", "appointment_id": str(appt.id), **_safety("NORMAL")}
        del body[missing]
        resp = _api().post(BOOKING_URL, body, format="json")
        assert resp.status_code == 400, resp.content
        assert not PlanStepBooking.objects.exists()

    def test_not_applicable_is_refused(self, ready) -> None:
        plan, appt = ready
        assert _link(plan, appt, **_safety("NOT_APPLICABLE")).status_code == 400
        assert not PlanStepBooking.objects.exists()

    def test_the_plan_saved_under_normal_stops_leading_to_a_booking_after_a_stop(self, ready) -> None:
        """Сама дыра листа: решение о плане принято при NORMAL, действие — при STOP."""
        plan, appt = ready
        assert PlanStepResolution.objects.get().safety_state == "NORMAL"
        assert _link(plan, appt, **_safety("STOP")).status_code == 409
        # Состояние сменилось обратно — то же действие проходит: гейт ничего не запомнил.
        assert _link(plan, appt, **_safety("NORMAL")).status_code == 201
        plan.refresh_from_db()
        assert plan.status == "active"

    def test_the_writer_itself_refuses_without_the_endpoint(self, ready) -> None:
        """Допуск внутри транзакции создания записи пойдёт через ``attach_booking``
        напрямую, мимо ручки, — гейт обязан стоять и там."""
        from wellness.plan_engine_steps import StepNotExecutable, attach_booking
        from wellness.plan_safety import SafetyInput

        plan, appt = ready
        with pytest.raises(StepNotExecutable) as exc:
            attach_booking(plan, "s1", appt, SafetyInput("UNKNOWN", "v", 0))
        assert exc.value.reason == "safety_blocked"
        assert not PlanStepBooking.objects.exists()
