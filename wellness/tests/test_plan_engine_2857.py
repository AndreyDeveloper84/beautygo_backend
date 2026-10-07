"""Durable Plan / PlanRevision — граница сохранения (DRF-2857, Plan WP1).

Контракт PLAN_ENGINE_CONTRACT v1.0 §4.4–§4.9. Приёмка листа: создание
идемпотентно; замена — атомарное гашение прежнего; не более одного ``active``
на цель; история Lite цела. Сверх неё — то, без чего приёмка пуста:

* ревизия иммутабельна на всех четырёх дверях ORM;
* форма шага и закрытый список §4.8 проверяются на границе хранения, отказ —
  целиком, без частичной записи. Список запрещённых полей здесь набран с
  текста контракта ЗАНОВО (``CONTRACT_4_8``), а не взят из константы кода:
  узел из константы не ловит константу;
* флаги: движок выключен — писатели 404, чтение ``null``; включён — писатель
  Lite отказывает, чтение и закрытие Lite живут; выключили обратно — Lite
  пишет, строки движка на месте;
* стирание сквозным путём — удаление аккаунта через ``execute()`` и «забудь
  всё»: планы и ревизии человека исчезают, чужие целы;
* гонки на двух соединениях: одна и та же команда → один план; две разные
  команды на одну цель → ровно один ``active``.
"""
from __future__ import annotations

import copy
import threading
import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from rest_framework.test import APIClient

from goals.models import ClientGoal
from users.models import User
from wellness.models import (
    PLAN_FORBIDDEN_FIELDS,
    ImmutablePlanRecordError,
    PersonalPlan,
    Plan,
    PlanAction,
    PlanRevision,
)
from wellness.plan_engine import (
    ContractViolation,
    create_plan_from_command,
    parse_command,
)

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-engine"
PLAN_URL = "/api/v1/internal/me/plan/"
STATE_URL = "/api/v1/internal/me/plan/state/"
LITE_URL = "/api/v1/internal/me/plan-lite/"
OWNER = "bot:plan-engine-owner"
STRANGER = "bot:plan-engine-stranger"

#: Закрытый список §4.8 — набран с контракта (v1.0, строки 295–298), 18 имён.
CONTRACT_4_8 = (
    "sub_steps", "children", "parts", "parent_step_id", "progress", "percent",
    "completed_count", "done", "score", "confidence", "next_visit_date", "course",
    "sessions_total", "sessions_done", "compatible_with", "incompatible_with",
    "repeat_every", "recovery_days",
)

POLICY_VERSIONS = {
    "plan_spec_version": "1.0",
    "constraint_policy_version": "1.0",
    "resolver_spec_version": "1.0",
    "catalog_mapping_version": "2026-10-07",
    "safety_policy_version": "0.12",
    "reason_code_registry_version": "3",
}


def _step(step_id: str = "s1", **over) -> dict:
    step = {
        "step_id": step_id,
        "role": "CORE",
        "outcome_ref": "relax",
        "capability_ref": "cap:relaxation-massage",
        "level": "CAPABILITY",
        "canonical_service_ref": None,
        "tenant_offer_ref": None,
        "assertions": ["a1"],
        "execution": None,
        "alternative_of": None,
        "provenance": {"built_at_revision": 1, "engine_version": "0.1"},
    }
    step.update(over)
    return step


def _verdicts(steps: list) -> dict:
    """DRF-2868: вердикт валидации обязателен по каждому шагу."""
    return {
        s["step_id"]: "INCOMPLETE"
        for s in steps
        if isinstance(s, dict) and isinstance(s.get("step_id"), str)
    }


def _command(goal: ClientGoal | None, *, steps: list[dict] | None = None, **over) -> dict:
    steps = steps if steps is not None else [_step("s1"), _step("s2", role="OPTIONAL")]
    body = {
        "decision_id": str(uuid.uuid4()),
        "goal_ref": str(goal.id) if goal is not None else None,
        "mode": "SAVE",
        "confirmation": {"question_id": "plan.save", "option_id": "yes", "state_revision": 7},
        "provenance": {"policy_versions": dict(POLICY_VERSIONS)},
        "decision": {
            "steps": steps,
            "assertions": [
                {"assertion_id": "a1", "kind": "SERVICE_CAPABILITY_MAPPING",
                 "value": {"state": "Unknown", "reason": "NO_RULE"}},
            ],
            "validation": {"status": "INCOMPLETE", "step_validations": _verdicts(steps)},
        },
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _token_and_flags(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_ENGINE_ENABLED = True
    settings.PLAN_LITE_ENABLED = True


def _user(username: str, phone: str) -> User:
    return User.objects.create_user(
        username=username, password="x", role="client", phone=phone, is_proxy=True,
    )


@pytest.fixture
def owner(db) -> User:
    return _user(OWNER, "+79995028571")


@pytest.fixture
def stranger(db) -> User:
    return _user(STRANGER, "+79995028572")


@pytest.fixture
def goal(owner) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key="relax", source_channel="bot")


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _save(goal: ClientGoal, user=None, **over) -> Plan:
    plan, created = create_plan_from_command(user or goal.client, parse_command(_command(goal, **over)))
    assert created
    return plan


# ─── 1. команда сохранения: идемпотентность и атомарность (§4.9) ─────────────


class TestCreateCommand:
    def test_saves_a_plan_with_its_first_revision(self, goal) -> None:
        body = _command(goal)
        resp = _api().post(PLAN_URL, body, format="json")
        assert resp.status_code == 201, resp.content
        data = resp.json()["data"]
        assert data["created"] is True
        plan = Plan.objects.get()
        assert (plan.status, plan.goal_id, plan.subject_user_id) == ("active", goal.id, goal.client_id)
        assert plan.created_via == "confirmation"
        revision = PlanRevision.objects.get()
        assert plan.current_revision_id == revision.id
        assert revision.revision_no == 1
        # Снимок, не ссылки: шаги лежат в ревизии целиком и такими, как пришли.
        assert revision.steps_snapshot == body["decision"]["steps"]
        assert revision.assertions_snapshot == body["decision"]["assertions"]
        assert revision.created_from == {"decision_id": body["decision_id"]}
        assert revision.staleness == "none"
        assert data["plan"]["revision"]["steps"] == body["decision"]["steps"]
        assert data["plan"]["in_effect"] is True

    def test_a_repeat_returns_the_same_plan_and_no_second_revision(self, goal) -> None:
        body = _command(goal)
        first = _api().post(PLAN_URL, body, format="json")
        second = _api().post(PLAN_URL, body, format="json")
        assert (first.status_code, second.status_code) == (201, 200)
        assert second.json()["data"]["created"] is False
        assert second.json()["data"]["plan"]["plan_id"] == first.json()["data"]["plan"]["plan_id"]
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)

    def test_the_same_confirmation_with_other_content_is_a_conflict(self, goal) -> None:
        body = _command(goal)
        assert _api().post(PLAN_URL, body, format="json").status_code == 201
        other = copy.deepcopy(body)
        other["decision"]["steps"] = [_step("s9")]
        other["decision"]["validation"]["step_validations"] = {"s9": "INCOMPLETE"}
        resp = _api().post(PLAN_URL, other, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_IDEMPOTENCY_CONFLICT"
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)
        assert PlanRevision.objects.get().steps_snapshot == body["decision"]["steps"]

    def test_the_key_is_the_servers_and_carries_the_confirmation(self, goal) -> None:
        """Другое подтверждение того же решения — другая команда, а не повтор."""
        body = _command(goal)
        again = copy.deepcopy(body)
        again["confirmation"]["state_revision"] = 8
        assert _api().post(PLAN_URL, body, format="json").status_code == 201
        assert _api().post(PLAN_URL, again, format="json").status_code == 201
        assert Plan.objects.count() == 2
        assert len(set(Plan.objects.values_list("idempotency_key", flat=True))) == 2

    def test_a_second_plan_for_the_goal_supersedes_the_first_in_place(self, goal) -> None:
        first = _save(goal)
        second = _save(goal)
        first.refresh_from_db()
        assert (first.status, second.status) == ("superseded", "active")
        # История цела: прежний план и его ревизия на месте.
        assert first.current_revision.revision_no == 1
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (2, 2)
        assert Plan.objects.filter(goal=goal, status="active").count() == 1

    def test_a_failure_midway_leaves_the_previous_plan_active(self, goal, monkeypatch) -> None:
        first = _save(goal)

        def _boom(*args, **kwargs):
            raise RuntimeError("revision write failed")

        monkeypatch.setattr(PlanRevision.objects, "create", _boom)
        with pytest.raises(RuntimeError):
            create_plan_from_command(goal.client, parse_command(_command(goal)))
        first.refresh_from_db()
        assert first.status == "active"
        assert Plan.objects.count() == 1

    def test_two_active_plans_for_one_goal_cannot_exist(self, goal) -> None:
        _save(goal)
        with pytest.raises(IntegrityError), transaction.atomic():
            Plan.objects.create(subject_user=goal.client, goal=goal, idempotency_key="k" * 64)

    def test_a_plan_without_a_goal_is_refused_by_the_writer(self, owner) -> None:
        """Q-PE-2 открыт: колонка nullable ради истории, писатель без цели не пишет."""
        resp = _api().post(PLAN_URL, _command(None), format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["details"]["reason"] == "goal_required"
        assert not Plan.objects.exists()

    @pytest.mark.parametrize("state", ["paused", "achieved", "archived", "superseded"])
    def test_a_goal_that_is_not_active_is_not_found(self, goal, state) -> None:
        ClientGoal.objects.filter(pk=goal.pk).update(state=state)
        resp = _api().post(PLAN_URL, _command(goal), format="json")
        assert resp.status_code == 404
        assert not Plan.objects.exists()

    def test_another_persons_goal_is_not_found_not_forbidden(self, goal, stranger) -> None:
        resp = _api(STRANGER).post(PLAN_URL, _command(goal), format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "goal_not_found"
        assert not Plan.objects.exists()

    def test_the_plan_outlives_its_goal_as_history(self, goal) -> None:
        plan = _save(goal)
        goal.delete()
        plan.refresh_from_db()
        assert plan.goal_id is None
        assert plan.current_revision.revision_no == 1


# ─── 2. форма на границе хранения (§4.2, §4.8) ───────────────────────────────


class TestContractShape:
    def test_the_forbidden_list_in_code_is_the_contracts(self) -> None:
        assert len(CONTRACT_4_8) == len(set(CONTRACT_4_8)) == 18
        assert set(CONTRACT_4_8) == PLAN_FORBIDDEN_FIELDS

    def test_no_model_field_is_on_the_forbidden_list(self) -> None:
        for model in (Plan, PlanRevision):
            names = {f.name for f in model._meta.get_fields()}
            assert names and not names & set(CONTRACT_4_8), model

    @pytest.mark.parametrize("field", CONTRACT_4_8)
    def test_a_forbidden_field_on_a_step_refuses_the_whole_command(self, goal, field) -> None:
        resp = _api().post(PLAN_URL, _command(goal, steps=[_step("s1", **{field: 1})]), format="json")
        assert resp.status_code == 400, field
        assert resp.json()["error"]["code"] == "PLAN_CONTRACT_VIOLATION"
        assert resp.json()["error"]["details"] == {"reason": "forbidden_field", "detail": field}
        assert not Plan.objects.exists()

    @pytest.mark.parametrize("field", CONTRACT_4_8)
    def test_a_forbidden_field_nested_in_a_step_is_refused(self, goal, field) -> None:
        step = _step("s1", execution={"required_params": [], "entry_point": "PLAN_STEP", "x": [{field: 1}]})
        with pytest.raises(ContractViolation) as exc:
            parse_command(_command(goal, steps=[step]))
        assert (exc.value.reason, exc.value.detail) == ("forbidden_field", field)

    @pytest.mark.parametrize("field", CONTRACT_4_8)
    def test_a_forbidden_field_in_the_validation_is_refused(self, goal, field) -> None:
        body = _command(goal)
        body["decision"]["validation"][field] = 1
        with pytest.raises(ContractViolation) as exc:
            parse_command(body)
        assert (exc.value.reason, exc.value.detail) == ("forbidden_field", field)

    def test_a_step_has_no_room_for_free_text(self, goal) -> None:
        """PE-2: незнакомый ключ шага — отказ, а не сохранённая приписка."""
        with pytest.raises(ContractViolation) as exc:
            parse_command(_command(goal, steps=[_step("s1", note="раз в две недели")]))
        assert (exc.value.reason, exc.value.detail) == ("step_unknown_key", "note")

    @pytest.mark.parametrize(
        ("steps", "reason"),
        [
            ([], "steps_empty"),
            (["s1"], "step_not_object"),
            ([_step("")], "step_id_missing"),
            ([_step("s1"), _step("s1")], "step_id_duplicate"),
            ([_step("s1", role="MAIN")], "step_role_invalid"),
            ([_step("s1", capability_ref="")], "capability_ref_missing"),
            ([_step("s1", capability_ref=None)], "capability_ref_missing"),
            ([_step("s1", level="VISIT")], "step_level_invalid"),
            ([_step("s1", canonical_service_ref="svc:1")], "step_level_refs_mismatch"),
            ([_step("s1", level="SERVICE")], "step_level_refs_mismatch"),
            ([_step("s1", level="SERVICE", canonical_service_ref="svc:1", tenant_offer_ref="o:1")],
             "step_level_refs_mismatch"),
            ([_step("s1", level="OFFER", canonical_service_ref="svc:1")], "step_level_refs_mismatch"),
            ([_step("s1", assertions="a1")], "step_assertions_malformed"),
            ([_step("s1", assertions=["a404"])], "assertion_not_in_snapshot"),
            ([_step("s1", role="ALTERNATIVE")], "alternative_of_invalid"),
            ([_step("s1", role="ALTERNATIVE", alternative_of="s1")], "alternative_of_invalid"),
            ([_step("s1", role="ALTERNATIVE", alternative_of="s404")], "alternative_of_invalid"),
            ([_step("s1"), _step("s2", alternative_of="s1")], "alternative_of_on_non_alternative"),
        ],
    )
    def test_each_step_rule_refuses_by_name(self, goal, steps, reason) -> None:
        with pytest.raises(ContractViolation) as exc:
            parse_command(_command(goal, steps=steps))
        assert exc.value.reason == reason

    def test_the_well_formed_shapes_pass(self, goal) -> None:
        steps = [
            _step("s1"),
            _step("s2", level="SERVICE", canonical_service_ref="svc:1"),
            _step("s3", level="OFFER", canonical_service_ref="svc:1", tenant_offer_ref="offer:1",
                  execution={"required_params": ["slot"], "entry_point": "PLAN_STEP"}),
            _step("s4", role="ALTERNATIVE", alternative_of="s1"),
        ]
        assert parse_command(_command(goal, steps=steps)).steps == steps

    @pytest.mark.parametrize(
        ("patch", "reason"),
        [
            ({"mode": "FOLLOW"}, "mode_not_supported"),
            ({"decision_id": "not-a-uuid"}, "decision_id_malformed"),
            ({"goal_ref": "not-a-uuid"}, "goal_ref_malformed"),
            ({"confirmation": None}, "confirmation_missing"),
            ({"confirmation": {"question_id": "q", "option_id": "", "state_revision": 1}},
             "confirmation_incomplete"),
            ({"confirmation": {"question_id": "q", "option_id": "o", "state_revision": True}},
             "confirmation_state_revision_malformed"),
            ({"confirmation": {"question_id": "q", "option_id": "o"}},
             "confirmation_state_revision_malformed"),
            ({"provenance": {}}, "policy_versions_malformed"),
            ({"provenance": {"policy_versions": {"plan_spec_version": "1.0"}}}, "policy_versions_malformed"),
            ({"provenance": {"policy_versions": {**POLICY_VERSIONS, "extra": "1"}}},
             "policy_versions_malformed"),
            ({"provenance": {"policy_versions": {**POLICY_VERSIONS, "plan_spec_version": ""}}},
             "policy_versions_malformed"),
            ({"decision": None}, "decision_missing"),
        ],
    )
    def test_each_command_rule_refuses_by_name(self, goal, patch, reason) -> None:
        with pytest.raises(ContractViolation) as exc:
            parse_command(_command(goal, **patch))
        assert exc.value.reason == reason

    @pytest.mark.parametrize("validation", [None, {}, {"status": "OK"}])
    def test_a_validation_without_a_contract_status_is_refused(self, goal, validation) -> None:
        body = _command(goal)
        body["decision"]["validation"] = validation
        with pytest.raises(ContractViolation) as exc:
            parse_command(body)
        assert exc.value.reason == "validation_malformed"

    @pytest.mark.parametrize("status", ["VALID", "INCOMPLETE", "BLOCKED"])
    def test_every_contract_validation_status_is_storable(self, goal, status) -> None:
        """INCOMPLETE — первоклассный результат (§6.3); BLOCKED сохраняется как
        замысел — допуск к исполнению решается не здесь."""
        body = _command(goal)
        validation = {"status": status, "step_validations": {"s1": status, "s2": "VALID"}}
        body["decision"]["validation"] = validation
        assert _api().post(PLAN_URL, body, format="json").status_code == 201
        assert PlanRevision.objects.get().validation == validation


# ─── 3. ревизия иммутабельна (§4.4, §9.1) ────────────────────────────────────


class TestRevisionIsImmutable:
    def test_all_four_doors_are_shut(self, goal) -> None:
        revision = _save(goal).current_revision
        revision.staleness = "stale_rules"
        with pytest.raises(ImmutablePlanRecordError):
            revision.save()
        with pytest.raises(ImmutablePlanRecordError):
            PlanRevision.objects.filter(pk=revision.pk).update(staleness="stale_rules")
        with pytest.raises(ImmutablePlanRecordError):
            PlanRevision.objects.bulk_update([revision], ["staleness"])
        with pytest.raises(ImmutablePlanRecordError):
            revision.delete()
        with pytest.raises(ImmutablePlanRecordError):
            PlanRevision.objects.filter(pk=revision.pk).delete()
        assert PlanRevision.objects.get(pk=revision.pk).staleness == "none"

    def test_revision_numbers_do_not_repeat_within_a_plan(self, goal) -> None:
        plan = _save(goal)
        with pytest.raises(IntegrityError), transaction.atomic():
            PlanRevision.objects.create(
                plan=plan, revision_no=1, steps_snapshot=[], validation={},
                policy_versions={}, created_from={}, content_hash="0" * 64,
            )


# ─── 4. статус по слову человека (§4.5) ──────────────────────────────────────


class TestHumanStatus:
    def _set(self, plan: Plan, state: str, who: str = OWNER):
        return _api(who).post(STATE_URL, {"plan_id": str(plan.id), "state": state}, format="json")

    def test_pause_resume_archive(self, goal) -> None:
        plan = _save(goal)
        for state in ("paused", "active", "archived"):
            resp = self._set(plan, state)
            assert resp.status_code == 200, (state, resp.content)
            plan.refresh_from_db()
            assert plan.status == state

    @pytest.mark.parametrize("terminal", ["archived", "superseded"])
    @pytest.mark.parametrize("to", ["active", "paused"])
    def test_terminal_states_have_no_way_back(self, goal, terminal, to) -> None:
        plan = _save(goal)
        Plan.objects.filter(pk=plan.pk).update(status=terminal)
        resp = self._set(plan, to)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_TRANSITION_REFUSED"
        plan.refresh_from_db()
        assert plan.status == terminal

    def test_superseded_cannot_be_requested(self, goal) -> None:
        plan = _save(goal)
        assert self._set(plan, "superseded").status_code == 400
        plan.refresh_from_db()
        assert plan.status == "active"

    def test_the_same_state_twice_is_not_an_error_and_not_a_write(self, goal) -> None:
        plan = _save(goal)
        assert self._set(plan, "paused").status_code == 200
        stamp = Plan.objects.get(pk=plan.pk).status_changed_at
        assert self._set(plan, "paused").status_code == 200
        assert Plan.objects.get(pk=plan.pk).status_changed_at == stamp

    def test_a_paused_plan_cannot_resume_over_a_newer_active_one(self, goal) -> None:
        paused = _save(goal)
        assert self._set(paused, "paused").status_code == 200
        newer = _save(goal)
        resp = self._set(paused, "active")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_ACTIVE_EXISTS"
        paused.refresh_from_db()
        newer.refresh_from_db()
        assert (paused.status, newer.status) == ("paused", "active")

    def test_another_persons_plan_is_not_found(self, goal, stranger) -> None:
        plan = _save(goal)
        assert self._set(plan, "archived", who=STRANGER).status_code == 404
        plan.refresh_from_db()
        assert plan.status == "active"


# ─── 5. чтение ───────────────────────────────────────────────────────────────


class TestRead:
    def test_no_plan_is_null(self, goal) -> None:
        assert _api().get(PLAN_URL).json()["data"] == {"plan": None}

    def test_the_active_plan_wins_over_a_paused_one(self, goal) -> None:
        paused = _save(goal)
        Plan.objects.filter(pk=paused.pk).update(status="paused")
        active = _save(goal)
        assert _api().get(PLAN_URL).json()["data"]["plan"]["plan_id"] == str(active.id)

    def test_a_paused_goal_puts_the_plan_out_of_reading_without_rewriting_it(self, goal) -> None:
        """Статус цели в статус плана не переписывается (§4.5 отложен 07.10)."""
        plan = _save(goal)
        ClientGoal.objects.filter(pk=goal.pk).update(state="paused")
        assert _api().get(PLAN_URL).json()["data"] == {"plan": None}
        plan.refresh_from_db()
        assert plan.status == "active"

    def test_a_stranger_reads_nothing(self, goal, stranger) -> None:
        _save(goal)
        assert _api(STRANGER).get(PLAN_URL).json()["data"] == {"plan": None}

    def test_the_document_expresses_no_progress(self, goal) -> None:
        _save(goal)
        doc = _api().get(PLAN_URL).json()["data"]["plan"]

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        found = set(keys(doc))
        assert "steps" in found  # присутствие раньше отсутствия
        assert not found & set(CONTRACT_4_8)


# ─── 6. флаги и переключение с Lite ──────────────────────────────────────────

LITE_BODY = {"actions": [{"action_type": "log_water", "cadence": "per_day", "target_count": 2}]}


class TestFlagsAndLite:
    def test_engine_off_writers_404_and_read_is_null(self, goal, settings) -> None:
        plan = _save(goal)
        settings.PLAN_ENGINE_ENABLED = False
        resp = _api().post(PLAN_URL, _command(goal), format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"
        state = _api().post(STATE_URL, {"plan_id": str(plan.id), "state": "paused"}, format="json")
        assert state.status_code == 404
        assert _api().get(PLAN_URL).json()["data"] == {"plan": None}
        # Откат флага ничего не удаляет.
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)
        assert Plan.objects.get().status == "active"

    def test_engine_on_the_lite_writer_refuses_and_lite_reading_lives(self, goal, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        lite = PersonalPlan.objects.get()
        # Действующий Lite-план читается и закрывается, пока человек не сохранил новый.
        from wellness.plan_lite import plan_lite_payload

        assert plan_lite_payload(goal.client)["plan_id"] == str(lite.id)
        assert _api().delete(LITE_URL).status_code == 200
        resp = _api().post(LITE_URL, LITE_BODY, format="json")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_LITE_SUPERSEDED_BY_ENGINE"
        assert PersonalPlan.objects.count() == 1

    def test_engine_off_again_lite_writes_and_engine_rows_stay(self, goal, settings) -> None:
        _save(goal)
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)

    def test_saving_a_plan_supersedes_the_active_lite_plan_and_keeps_its_rows(self, goal, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        lite = PersonalPlan.objects.get()
        before = list(PlanAction.objects.filter(plan=lite).values())
        assert before  # присутствие раньше отсутствия

        _save(goal)

        lite.refresh_from_db()
        # Не «закрыт пользователем»: человек подтвердил новый план, старый не отвергал.
        assert lite.status == "superseded"
        assert lite.closed_at is not None
        assert list(PlanAction.objects.filter(plan=lite).values()) == before
        assert not PersonalPlan.objects.filter(status="active").exists()

    def test_closed_lite_history_is_untouched(self, goal, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        assert _api().delete(LITE_URL).status_code == 200
        settings.PLAN_ENGINE_ENABLED = True
        before = (list(PersonalPlan.objects.values()), list(PlanAction.objects.values()))

        _save(goal)

        assert (list(PersonalPlan.objects.values()), list(PlanAction.objects.values())) == before
        assert before[0][0]["status"] == "closed_by_user"

    def test_a_strangers_lite_plan_is_not_superseded(self, goal, stranger, settings) -> None:
        ClientGoal.objects.create(client=stranger, goal_key="relax", source_channel="bot")
        settings.PLAN_ENGINE_ENABLED = False
        assert _api(STRANGER).post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        _save(goal)
        assert PersonalPlan.objects.get(user=stranger).status == "active"

    def test_a_refused_command_does_not_touch_the_lite_plan(self, goal, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        assert _api().post(LITE_URL, LITE_BODY, format="json").status_code == 201
        settings.PLAN_ENGINE_ENABLED = True
        resp = _api().post(PLAN_URL, _command(goal, steps=[_step("s1", done=True)]), format="json")
        assert resp.status_code == 400
        assert PersonalPlan.objects.get().status == "active"

    def test_the_flag_is_closed_by_default(self) -> None:
        from pathlib import Path

        base = Path(__file__).resolve().parents[2] / "djangoProject" / "settings" / "base.py"
        line = 'PLAN_ENGINE_ENABLED = os.environ.get("PLAN_ENGINE_ENABLED", "false").lower() == "true"'
        assert base.read_text(encoding="utf-8").count(line) == 1


# ─── 7. стирание и выгрузка — сквозным путём ─────────────────────────────────


class _BotOk:
    def confirm(self, **kw):
        from users.deletion_executor import BotConfirmation

        return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})


class TestErasureAndExport:
    @pytest.fixture
    def both(self, goal, stranger):
        theirs = ClientGoal.objects.create(client=stranger, goal_key="relax", source_channel="bot")
        _save(goal)
        _save(goal)  # вторая — чтобы у человека были и superseded, и active
        _save(theirs)
        assert Plan.objects.filter(subject_user=goal.client).count() == 2
        assert PlanRevision.objects.filter(plan__subject_user=goal.client).count() == 2
        return goal.client, stranger

    def _only_the_strangers_remain(self, person, stranger) -> None:
        assert not Plan.objects.filter(subject_user=person).exists()
        assert not PlanRevision.objects.filter(plan__subject_user=person).exists()
        assert Plan.objects.filter(subject_user=stranger).count() == 1
        assert PlanRevision.objects.filter(plan__subject_user=stranger).count() == 1

    def test_account_deletion_erases_plans_and_revisions(self, both) -> None:
        from users.deletion_executor import execute
        from users.deletion_requests import ensure_deletion_request

        person, stranger = both
        req = ensure_deletion_request(person, initiator="bot").request
        out = execute(req, bot_client=_BotOk())
        assert out.completed, getattr(req, "failure_reason", "")
        self._only_the_strangers_remain(person, stranger)

    def test_forget_all_erases_plans_and_revisions(self, both) -> None:
        from users.forget_all_catalog import REMEMBERED_SCOPE, erase_remembered_catalog

        person, stranger = both
        counts = erase_remembered_catalog(person, initiator="test")
        # Два плана + две ревизии каскадом.
        assert counts["wellness.Plan"] == 4
        assert REMEMBERED_SCOPE["wellness.Plan"] == "wellness_plan"
        self._only_the_strangers_remain(person, stranger)

    def test_the_pointer_is_decided_in_the_deletion_registry(self) -> None:
        from users.deletion_executor import DELETE, pointers_to_user, undecided_pointers

        assert "wellness.Plan.subject_user" in pointers_to_user()
        assert "wellness.Plan.subject_user" in DELETE
        assert undecided_pointers() == {}

    def test_the_export_carries_the_saved_plan_and_no_service_keys(self, both) -> None:
        from users.remembered_export import export_wellness_plan

        person, _ = both
        saved = export_wellness_plan(person)["saved_plans"]
        assert [p["status"] for p in saved] == ["superseded", "active"]
        assert saved[0]["goal_key"] == "relax"
        assert saved[1]["revisions"][0]["steps"][0]["capability_ref"] == "cap:relaxation-massage"
        flat = repr(saved)
        assert "idempotency_key" not in flat and "content_hash" not in flat

    def test_account_reset_takes_the_plan_apart_by_design(self) -> None:
        from users.account_reset import MODES

        assert all("wellness.Plan.subject_user" in m.dismantles for m in MODES.values())


# ─── 8. гонки — два соединения ───────────────────────────────────────────────


def _in_threads(*calls):
    results: list = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def run(i, call):
        try:
            barrier.wait(timeout=10)
            results[i] = call()
        except Exception as exc:  # noqa: BLE001 — исход потока и есть предмет узла
            results[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


@pytest.mark.django_db(transaction=True)
class TestRaces:
    def test_the_same_command_twice_at_once_is_one_plan(self) -> None:
        user = _user(OWNER, "+79995028571")
        goal = ClientGoal.objects.create(client=user, goal_key="relax", source_channel="bot")
        body = _command(goal)

        def call():
            return create_plan_from_command(user, parse_command(copy.deepcopy(body)))

        results = _in_threads(call, call)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert sorted(created for _, created in results) == [False, True]
        assert len({plan.id for plan, _ in results}) == 1
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)

    def test_two_different_commands_at_once_leave_exactly_one_active(self) -> None:
        user = _user(OWNER, "+79995028571")
        goal = ClientGoal.objects.create(client=user, goal_key="relax", source_channel="bot")

        def call():
            return create_plan_from_command(user, parse_command(_command(goal)))

        results = _in_threads(call, call)
        assert not [r for r in results if isinstance(r, Exception)], results
        assert Plan.objects.count() == 2
        assert sorted(Plan.objects.values_list("status", flat=True)) == ["active", "superseded"]
        assert PlanRevision.objects.count() == 2
