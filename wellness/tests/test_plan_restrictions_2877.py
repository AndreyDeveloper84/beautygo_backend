# flake8: noqa: F811 — фикстуры импортированы из узлов шагов плана и приходят параметрами
"""Ограничения плана — стойкий незакрытый вопрос (DRF-2877).

Решение владельца 08.10.2026:

* «уточнить» — конкретный незакрытый вопрос, который не исчезает от
  следующего сообщения;
* ограничение затрагивает зависимый шаг или весь план — по области причины;
* черновик при «уточнить» сохраняется вместе с ограничениями; «стоп»
  сохранение блокирует.

Каталог хранит ограничение и отказывает действиям с шагом; вопрос задаёт и
оценивает бот. Что держат узлы: открытое ограничение не снимается ничем, кроме
явного снятия; блокирует действия с шагом и ничего сверх; повтор не плодит
строк; история не переписывается.
"""
from __future__ import annotations

import uuid

import pytest

from wellness.models import ImmutablePlanRecordError, Plan, PlanRestriction, PlanRestrictionLift, PlanRevision
from wellness.plan_restrictions import CAUSES, Cause
from wellness.tests.test_plan_engine_steps_2868 import (  # noqa: F401 — фикстуры того же сценария
    BOOKING_URL,
    OWNER,
    PLAN_URL,
    SAFETY,
    STRANGER,
    _api,
    _admission_is_not_the_subject,
    _book,
    _command,
    _link,
    _resolve,
    _save,
    _step,
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

RESTRICTIONS_URL = "/api/v1/internal/me/plan/restrictions/"
LIFT_URL = "/api/v1/internal/me/plan/restrictions/lift/"
STATE_URL = "/api/v1/internal/me/plan/state/"
QUESTION = "plan.safety_clarify"
#: Причина области «весь план» — только для узлов: таблица причин каталога
#: пуста до решения владельца, а механизм проверить нужно.
PLAN_CAUSE = "TEST_PLAN_QUESTION"
CLARIFY = {"scope": "PLAN", "cause": PLAN_CAUSE, "question_id": QUESTION}
#: Причина области «шаг» — только для узлов: в таблице причин её сегодня нет
#: (ждёт решения владельца), а механизм области «шаг» проверить нужно.
STEP_CAUSE = "TEST_STEP_QUESTION"


@pytest.fixture(autouse=True)
def plan_cause(monkeypatch):
    monkeypatch.setitem(CAUSES, PLAN_CAUSE, Cause(scopes=frozenset({"PLAN"}), lift_kinds=frozenset({"answered"})))


@pytest.fixture
def step_cause(monkeypatch):
    monkeypatch.setitem(CAUSES, STEP_CAUSE, Cause(scopes=frozenset({"STEP"}), lift_kinds=frozenset({"answered"})))


@pytest.fixture
def no_exit_cause(monkeypatch):
    """Причина без условия снятия — как состояния, у которых выхода нет."""
    monkeypatch.setitem(CAUSES, "TEST_NO_EXIT", Cause(scopes=frozenset({"PLAN"}), lift_kinds=frozenset()))


def _open(plan: Plan, who: str = OWNER, **over):
    return _api(who).post(RESTRICTIONS_URL, {"plan_id": str(plan.id), **CLARIFY, **SAFETY, **over}, format="json")


def _lift(plan: Plan, restriction_id: str, who: str = OWNER, **over):
    body = {
        "plan_id": str(plan.id), "restriction_id": restriction_id, "lift_kind": "answered",
        "answer_option_id": "opt-no", **SAFETY, **over,
    }
    return _api(who).post(LIFT_URL, body, format="json")


def _document() -> dict:
    return _api().get(PLAN_URL).json()["data"]["plan"]


class TestTheTableOfCausesIsEmptyUntilTheOwnerDecides:
    """Владелец 08.10: «Универсальный вопрос CLARIFY без причины придумывать
    не будем». В каталоге нет ни одной причины, и вердикт «уточнить» сам её
    не создаёт."""

    def test_the_catalog_ships_no_cause_of_its_own(self) -> None:
        import wellness.plan_restrictions as module

        assert set(CAUSES) == {PLAN_CAUSE}  # только подставленная этим файлом
        assert "SAFETY_CLARIFY" not in CAUSES
        assert not hasattr(module, "CAUSE_SAFETY_CLARIFY")

    @pytest.mark.parametrize("cause", ["SAFETY_CLARIFY", "CLARIFY", "HEALTH_QUESTION"])
    def test_a_cause_nobody_decided_cannot_be_opened(self, goal, cause) -> None:
        resp = _open(_save(goal), cause=cause)
        assert resp.status_code == 400
        assert resp.json()["error"]["details"]["reason"] == "restriction_cause_unknown"
        assert not PlanRestriction.objects.exists()


class TestSavingWithTheTurnVerdict:
    def test_silence_about_safety_is_not_normal(self, goal) -> None:
        body = _command(goal, [_step("s1")])
        for key in ("safety_state", "safety_policy_version", "evaluated_at_revision"):
            body.pop(key)
        resp = _api().post(PLAN_URL, body, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["details"]["reason"] == "safety_state_invalid"
        assert not Plan.objects.exists()

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_stop_and_unknown_block_the_save(self, goal, state) -> None:
        resp = _api().post(PLAN_URL, {**_command(goal, [_step("s1")]), "safety_state": state}, format="json")
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_SAVE_SAFETY_BLOCKED"
        assert not Plan.objects.exists() and not PlanRevision.objects.exists()

    def test_stop_blocks_even_with_a_restriction_attached(self, goal) -> None:
        """«Стоп» — не вопрос с ответом: ограничением его не оформить."""
        body = {**_command(goal, [_step("s1")]), "safety_state": "STOP", "restrictions": [CLARIFY]}
        assert _api().post(PLAN_URL, body, format="json").json()["error"]["code"] == "PLAN_SAVE_SAFETY_BLOCKED"
        assert not Plan.objects.exists()

    def test_clarify_alone_neither_blocks_the_save_nor_invents_a_question(self, goal) -> None:
        """«Уточнить» сохранение не блокирует; ограничение из самого вердикта
        не возникает — его открывает конкретная причина."""
        resp = _api().post(PLAN_URL, {**_command(goal, [_step("s1")]), "safety_state": "CLARIFY"}, format="json")
        assert resp.status_code == 201, resp.content
        assert resp.json()["data"]["plan"]["restrictions"] == []
        assert not PlanRestriction.objects.exists()

    def test_clarify_saves_the_draft_together_with_its_restriction(self, goal) -> None:
        body = {**_command(goal, [_step("s1"), _step("s2")]), "safety_state": "CLARIFY", "restrictions": [CLARIFY]}
        resp = _api().post(PLAN_URL, body, format="json")
        assert resp.status_code == 201, resp.content
        plan = resp.json()["data"]["plan"]
        assert plan["status"] == "active"  # отдельного статуса «черновик» нет
        assert [(r["scope"], r["step_id"], r["cause"], r["question_id"], r["lifted"]) for r in plan["restrictions"]] == [
            ("PLAN", None, PLAN_CAUSE, QUESTION, None),
        ]
        assert {s: v["restricted"] for s, v in plan["step_state"].items()} == {"s1": True, "s2": True}
        row = PlanRestriction.objects.get()
        assert (row.safety_state, row.safety_policy_version, row.safety_evaluated_at_revision) == (
            "CLARIFY", SAFETY["safety_policy_version"], SAFETY["evaluated_at_revision"],
        )

    def test_a_repeat_of_the_save_does_not_duplicate_the_restriction(self, goal) -> None:
        body = {**_command(goal, [_step("s1")]), "safety_state": "CLARIFY", "restrictions": [CLARIFY]}
        assert _api().post(PLAN_URL, body, format="json").status_code == 201
        again = _api().post(PLAN_URL, body, format="json")
        assert again.status_code == 200 and again.json()["data"]["created"] is False
        assert (Plan.objects.count(), PlanRestriction.objects.count()) == (1, 1)

    def test_a_repeat_of_a_saved_command_is_not_judged_again_under_stop(self, goal) -> None:
        body = _command(goal, [_step("s1")])
        assert _api().post(PLAN_URL, body, format="json").status_code == 201
        again = _api().post(PLAN_URL, {**body, "safety_state": "STOP"}, format="json")
        assert again.status_code == 200 and again.json()["data"]["created"] is False

    def test_an_ordinary_save_carries_no_restrictions(self, goal) -> None:
        resp = _api().post(PLAN_URL, _command(goal, [_step("s1")]), format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["plan"]["restrictions"] == []
        assert resp.json()["data"]["plan"]["step_state"]["s1"]["restricted"] is False

    @pytest.mark.parametrize(
        "restrictions, reason",
        [
            ("nope", "restrictions_malformed"),
            ([{"scope": "ALL", "cause": PLAN_CAUSE, "question_id": QUESTION}], "restriction_scope_invalid"),
            ([{"scope": "PLAN", "cause": "INVENTED", "question_id": QUESTION}], "restriction_cause_unknown"),
            ([{"scope": "STEP", "step_id": "s1", "cause": PLAN_CAUSE, "question_id": QUESTION}],
             "restriction_scope_not_for_cause"),
            ([{"scope": "PLAN", "step_id": "s1", "cause": PLAN_CAUSE, "question_id": QUESTION}],
             "restriction_step_id_on_plan_scope"),
            ([{"scope": "PLAN", "cause": PLAN_CAUSE, "question_id": " "}], "restriction_question_id_missing"),
            ([CLARIFY, CLARIFY], "restriction_duplicate"),
        ],
    )
    def test_a_malformed_restriction_refuses_the_whole_save(self, goal, restrictions, reason) -> None:
        resp = _api().post(PLAN_URL, {**_command(goal, [_step("s1")]), "restrictions": restrictions}, format="json")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason
        assert not Plan.objects.exists()

    def test_a_step_restriction_must_name_a_step_of_this_plan(self, goal, step_cause) -> None:
        step = {"scope": "STEP", "step_id": "ghost", "cause": STEP_CAUSE, "question_id": "q.step"}
        resp = _api().post(PLAN_URL, {**_command(goal, [_step("s1")]), "restrictions": [step]}, format="json")
        assert resp.json()["error"]["details"]["reason"] == "restriction_step_not_in_plan"
        assert not Plan.objects.exists()


class TestAnOpenRestrictionBlocksStepActionsAndNothingElse:
    def test_plan_scope_blocks_every_step(self, goal, canon, offer) -> None:
        plan = _save(goal)
        assert _open(plan).status_code == 201
        for step_id in ("s1", "s2"):
            resp = _resolve(plan, step_id, level="OFFER", canonical_service_ref=str(canon.id),
                            tenant_offer_ref=str(offer.id))
            assert resp.status_code == 409, resp.content
            assert resp.json()["error"]["code"] == "PLAN_STEP_NOT_EXECUTABLE"
            assert resp.json()["error"]["details"] == {"reason": "restriction_open", "question_ids": [QUESTION]}

    def test_step_scope_blocks_its_own_step_only(self, goal, canon, offer, step_cause) -> None:
        plan = _save(goal)
        opened = _open(plan, scope="STEP", step_id="s1", cause=STEP_CAUSE, question_id="q.step")
        assert opened.status_code == 201, opened.content
        refused = _resolve(plan, "s1", level="OFFER", canonical_service_ref=str(canon.id),
                           tenant_offer_ref=str(offer.id))
        assert refused.json()["error"]["details"] == {"reason": "restriction_open", "question_ids": ["q.step"]}
        assert _to_offer(plan, canon, offer, step_id="s2").status_code == 201
        assert {s: v["restricted"] for s, v in _document()["step_state"].items()} == {"s1": True, "s2": False}

    def test_a_booking_from_the_step_is_blocked_too(self, goal, canon, offer, specialist, owner) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        assert _open(plan).status_code == 201
        resp = _link(plan, _book(owner, specialist, offer))
        assert resp.status_code == 409
        assert resp.json()["error"]["details"] == {"reason": "restriction_open", "question_ids": [QUESTION]}

    def test_reading_the_plan_is_never_blocked(self, goal) -> None:
        plan = _save(goal)
        _open(plan)
        doc = _document()
        assert doc["plan_id"] == str(plan.id) and doc["in_effect"] is True
        assert len(doc["restrictions"]) == 1

    @pytest.mark.parametrize("state", ["paused", "archived"])
    def test_pausing_and_archiving_are_never_blocked(self, goal, state) -> None:
        plan = _save(goal)
        _open(plan)
        resp = _api().post(STATE_URL, {"plan_id": str(plan.id), "state": state}, format="json")
        assert resp.status_code == 200, resp.content

    def test_the_next_turn_with_a_normal_verdict_lifts_nothing(self, goal, canon, offer) -> None:
        """Решение владельца: вопрос не исчезает просто после следующего
        сообщения. Действие с НОРМАЛЬНЫМ вердиктом хода по-прежнему отказано."""
        plan = _save(goal)
        _open(plan, safety_state="CLARIFY")
        for _ in range(2):
            resp = _resolve(plan, "s1", level="OFFER", canonical_service_ref=str(canon.id),
                            tenant_offer_ref=str(offer.id))  # SAFETY несёт NORMAL
            assert resp.json()["error"]["details"]["reason"] == "restriction_open"
        assert not PlanRestrictionLift.objects.exists()

    def test_opening_does_not_change_the_plan_or_its_revision(self, goal) -> None:
        plan = _save(goal)
        before = {k: v for k, v in _document().items() if k not in ("restrictions", "step_state")}
        _open(plan)
        after = {k: v for k, v in _document().items() if k not in ("restrictions", "step_state")}
        assert after == before


class TestOpening:
    def test_a_repeat_is_the_same_row(self, goal) -> None:
        plan = _save(goal)
        first, again = _open(plan), _open(plan)
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json()["data"]["restriction_id"] == again.json()["data"]["restriction_id"]
        assert again.json()["data"]["created"] is False
        assert PlanRestriction.objects.count() == 1

    def test_the_same_question_can_be_opened_again_after_it_was_lifted(self, goal) -> None:
        plan = _save(goal)
        first = _open(plan).json()["data"]["restriction_id"]
        assert _lift(plan, first).status_code == 200
        second = _open(plan)
        assert second.status_code == 201
        assert second.json()["data"]["restriction_id"] != first
        assert [r["lifted"] is None for r in _document()["restrictions"]] == [False, True]

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN", "CLARIFY", "NORMAL"])
    def test_a_restriction_can_be_opened_under_any_verdict(self, goal, state) -> None:
        """Ограничение только сужает — открыть его можно и при «стоп»."""
        assert _open(_save(goal), safety_state=state).status_code == 201

    def test_a_strangers_plan_is_not_found(self, goal, stranger) -> None:
        resp = _open(_save(goal), who=STRANGER)
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "plan_not_found"
        assert not PlanRestriction.objects.exists()

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"plan_id": "nope"}, "plan_id_malformed"),
            ({"scope": "STEP"}, "restriction_scope_not_for_cause"),
            ({"cause": "INVENTED"}, "restriction_cause_unknown"),
            ({"question_id": ""}, "restriction_question_id_missing"),
            ({"safety_state": "NOT_APPLICABLE"}, "safety_state_invalid"),
            ({"evaluated_at_revision": -1}, "safety_revision_malformed"),
        ],
    )
    def test_malformed_requests_by_name(self, goal, over, reason) -> None:
        resp = _open(_save(goal), **over)
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason
        assert not PlanRestriction.objects.exists()

    def test_a_step_restriction_on_an_unknown_step_is_refused(self, goal, step_cause) -> None:
        resp = _open(_save(goal), scope="STEP", step_id="ghost", cause=STEP_CAUSE)
        assert resp.status_code == 400
        assert resp.json()["error"]["details"]["reason"] == "restriction_step_not_in_plan"

    def test_engine_off_404(self, goal, settings) -> None:
        plan = _save(goal)
        settings.PLAN_ENGINE_ENABLED = False
        assert _open(plan).json()["error"]["code"] == "PLAN_ENGINE_DISABLED"


class TestLifting:
    @pytest.fixture
    def restricted(self, goal):
        plan = _save(goal)
        return plan, _open(plan).json()["data"]["restriction_id"]

    def test_lifting_reopens_the_step(self, restricted, canon, offer) -> None:
        plan, rid = restricted
        resp = _lift(plan, rid)
        assert resp.status_code == 200, resp.content
        lifted = resp.json()["data"]["plan"]["restrictions"][0]["lifted"]
        assert lifted["lift_kind"] == "answered" and lifted["lifted_at"]
        assert _to_offer(plan, canon, offer).status_code == 201
        assert _document()["step_state"]["s1"]["restricted"] is False

    def test_a_repeat_of_the_lift_is_the_same_row(self, restricted) -> None:
        plan, rid = restricted
        first, again = _lift(plan, rid), _lift(plan, rid)
        assert (first.status_code, again.status_code) == (200, 200)
        assert (first.json()["data"]["created"], again.json()["data"]["created"]) == (True, False)
        assert PlanRestrictionLift.objects.count() == 1

    def test_a_repeat_after_the_lift_is_not_judged_again_under_stop(self, restricted) -> None:
        plan, rid = restricted
        _lift(plan, rid)
        assert _lift(plan, rid, safety_state="STOP").status_code == 200

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_a_blocking_verdict_cannot_lift(self, restricted, state) -> None:
        plan, rid = restricted
        resp = _lift(plan, rid, safety_state=state)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_RESTRICTION_NOT_LIFTABLE"
        assert resp.json()["error"]["details"]["reason"] == "safety_blocked"
        assert not PlanRestrictionLift.objects.exists()

    def test_a_lift_kind_the_cause_does_not_allow_is_refused(self, restricted) -> None:
        plan, rid = restricted
        resp = _lift(plan, rid, lift_kind="expired")
        assert resp.json()["error"]["details"]["reason"] == "lift_kind_not_allowed"
        assert not PlanRestrictionLift.objects.exists()

    def test_a_cause_without_a_lift_condition_is_never_lifted(self, goal, no_exit_cause) -> None:
        """Состояния, у которых выхода нет: ни ответом, ни чем другим."""
        plan = _save(goal)
        rid = _open(plan, cause="TEST_NO_EXIT", question_id="q.no_exit").json()["data"]["restriction_id"]
        resp = _lift(plan, rid)
        assert resp.status_code == 409
        assert resp.json()["error"]["details"]["reason"] == "no_lift_condition"

    def test_only_the_named_restriction_is_lifted(self, goal, canon, offer, step_cause) -> None:
        plan = _save(goal)
        whole = _open(plan).json()["data"]["restriction_id"]
        _open(plan, scope="STEP", step_id="s1", cause=STEP_CAUSE, question_id="q.step")
        assert _lift(plan, whole).status_code == 200
        refused = _resolve(plan, "s1", level="OFFER", canonical_service_ref=str(canon.id),
                           tenant_offer_ref=str(offer.id))
        assert refused.json()["error"]["details"] == {"reason": "restriction_open", "question_ids": ["q.step"]}
        assert _to_offer(plan, canon, offer, step_id="s2").status_code == 201

    def test_an_unknown_restriction_and_a_strangers_plan_are_not_found(self, restricted, stranger, goal) -> None:
        plan, rid = restricted
        unknown = _lift(plan, str(uuid.uuid4()))
        assert (unknown.status_code, unknown.json()["error"]["details"]["reason"]) == (404, "restriction_not_found")
        theirs = _lift(plan, rid, who=STRANGER)
        assert (theirs.status_code, theirs.json()["error"]["details"]["reason"]) == (404, "plan_not_found")
        assert not PlanRestrictionLift.objects.exists()

    def test_a_restriction_of_another_plan_is_not_found(self, restricted, goal) -> None:
        plan, rid = restricted
        newer = _save(goal)  # прежний план замещён, ограничение осталось при нём
        resp = _lift(newer, rid)
        assert resp.json()["error"]["details"]["reason"] == "restriction_not_found"

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"restriction_id": "nope"}, "restriction_id_malformed"),
            ({"lift_kind": ""}, "lift_kind_missing"),
            ({"answer_option_id": 7}, "answer_option_id_malformed"),
            ({"safety_policy_version": ""}, "safety_policy_version_missing"),
        ],
    )
    def test_malformed_requests_by_name(self, restricted, over, reason) -> None:
        plan, rid = restricted
        resp = _lift(plan, over.pop("restriction_id", rid), **over)
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason


class TestHistoryIsNotRewritten:
    def test_the_rows_are_append_only(self, goal) -> None:
        plan = _save(goal)
        rid = _open(plan).json()["data"]["restriction_id"]
        _lift(plan, rid)
        row, lift = PlanRestriction.objects.get(), PlanRestrictionLift.objects.get()
        for obj in (row, lift):
            with pytest.raises(ImmutablePlanRecordError):
                obj.save()
            with pytest.raises(ImmutablePlanRecordError):
                obj.delete()

    def test_a_lifted_restriction_stays_in_the_document(self, goal) -> None:
        plan = _save(goal)
        rid = _open(plan).json()["data"]["restriction_id"]
        _lift(plan, rid)
        doc = _document()["restrictions"]
        assert [(r["restriction_id"], r["lifted"]["lift_kind"]) for r in doc] == [(rid, "answered")]

    def test_no_words_of_the_person_are_stored(self, goal) -> None:
        """В строках только идентификаторы: ни текста вопроса, ни ответа."""
        fields = {f.name for f in PlanRestriction._meta.get_fields()} | {
            f.name for f in PlanRestrictionLift._meta.get_fields()
        }
        assert not fields & {"text", "question_text", "answer", "answer_text", "message", "comment", "note"}

    def test_the_restrictions_go_with_the_plan_when_the_person_is_erased(self, goal, owner) -> None:
        plan = _save(goal)
        rid = _open(plan).json()["data"]["restriction_id"]
        _lift(plan, rid)
        Plan.objects.filter(pk=plan.pk).delete()
        assert not PlanRestriction.objects.exists() and not PlanRestrictionLift.objects.exists()
