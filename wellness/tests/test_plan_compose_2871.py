"""Сборка и проверка плана на уровне способностей (DRF-2871, Plan WP4).

Контракт PLAN_ENGINE_CONTRACT v1.0 §1.2, §4.1–§4.2, §5, §6, §12; контракт
планировочных ограничений v1.0 §2–§5, §8. Что держат узлы:

* шаги — только из ПОДТВЕРЖДЁННОГО знания «способность помогает цели»: вывод
  системы, «не поддержано», истёкшее и неактивная цель шагов не дают;
* роли и уровень: ни одного ``CORE``, ни одной ``ALTERNATIVE``, всё
  ``CAPABILITY`` — знания об обязательности нет;
* исходы без плана — штатные ответы с именем: безопасность, нет цели, нет
  декомпозиции, план не оправдан;
* утверждения — только из присланного реестра, каждое с провенансом; вход
  без провенанса или без значения отвергается целиком;
* сквозное: решение принимается командой сохранения WP1 БЕЗ переделки —
  узел сохраняет его настоящей ручкой;
* курс и горизонт со связи в решение не попадают, хотя в базе они есть.

Реестр в узлах — выжимка той же формы, что вендорится в бот
(``apps/planning_rules/data/planning-rules-registry.yaml``): два правила с
субъектом «способность» и одно с субъектом «канон», все ``UNKNOWN``.
"""
from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from goals.models import ClientGoal
from services.models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    ServiceCategory,
    ServiceTemplate,
)
from users.models import User
from wellness.models import PLAN_FORBIDDEN_FIELDS, Plan, PlanRevision
from wellness.plan_engine import STEP_KEYS

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-compose"
DECISION_URL = "/api/v1/internal/me/plan/decision/"
PLAN_URL = "/api/v1/internal/me/plan/"
OWNER = "bot:plan-compose-owner"
STRANGER = "bot:plan-compose-stranger"
SOURCE = "ayla-knowledge:03 AI System/Contracts/planning-rules-registry.yaml"


def _rule(kind: str, subject_kind: str, **over) -> dict:
    rule = {
        "rule_id": f"PR-{kind}-0001",
        "kind": kind,
        "status": "UNKNOWN",
        "value": {"reason": "NO_RULE_EXISTS", "asked_source": SOURCE, "as_of": "2026-09-08"},
        "unit": None,
        "applicability": {"scope": "GENERAL", "subject_kind": subject_kind, "subject_ids": [], "conditions": []},
        "provenance": {"source": SOURCE, "version": "0.1"},
    }
    rule.update(over)
    return rule


def _registry() -> dict:
    return {
        "registry_version": "0.1",
        "rules": [
            _rule("CAPABILITY_SEMANTICS", "capability"),
            _rule("SERVICE_CAPABILITY_MAPPING", "capability"),
            _rule("SAFETY_CONSTRAINT", "canonical_service"),
        ],
    }


def _body(**over) -> dict:
    body = {"safety_state": "NORMAL", "safety_policy_version": "safety-digest-1", "rules_registry": _registry()}
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_ENGINE_ENABLED = True


def _user(username: str, phone: str, **kw) -> User:
    return User.objects.create_user(username=username, password="x", role="client", phone=phone, **kw)


@pytest.fixture
def owner(db) -> User:
    return _user(OWNER, "+79995028711", is_proxy=True)


@pytest.fixture
def curator(db) -> User:
    return _user("plan_compose_curator", "+79995028713")


@pytest.fixture
def relax(db) -> GoalOption:
    return GoalOption.objects.create(key="relax-2871", label="Расслабиться")


@pytest.fixture
def goal(owner, relax) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key=relax.key, source_channel="bot")


@pytest.fixture
def category(db) -> ServiceCategory:
    return ServiceCategory.objects.create(name="Массаж 2871", slug="massage-2871")


@pytest.fixture
def back(category) -> ServiceTemplate:
    return ServiceTemplate.objects.create(category=category, name="Массаж спины 2871")


@pytest.fixture
def body_massage(category) -> ServiceTemplate:
    return ServiceTemplate.objects.create(category=category, name="Массаж тела 2871")


def _approved(curator: User) -> dict:
    return dict(
        status=ClaimEvidence.Status.APPROVED,
        claim_type=ClaimEvidence.ClaimType.PRODUCT,
        evidence_kind=ClaimEvidence.EvidenceKind.PROFESSIONAL_CONSENSUS,
        claim_scope=ClaimEvidence.ClaimScope.SUPPORTED,
        confirmed_by=curator,
        confirmed_at=timezone.now() - timedelta(days=1),
        source_ref="owner-review-2871",
    )


def _capability(template, key: str, curator, goal_option, *, cap=None, link=None, with_link: bool = True):
    """Подтверждённая способность с подтверждённой связью на цель; ``cap`` и
    ``link`` перекрывают поля основания у способности и у связи порознь.
    ``template`` — одна процедура или список: способность — запись общего
    словаря (DRF-2743), привязанная к нескольким процедурам."""
    templates = list(template) if isinstance(template, (list, tuple)) else [template]
    capability = ProcedureCapability.objects.create(
        templates=templates, key=key, text_client=f"Помогает: {key}", **{**_approved(curator), **(cap or {})},
    )
    if with_link:
        CapabilityGoalLink.objects.create(
            capability=capability, goal=goal_option, **{**_approved(curator), **(link or {})},
        )
    return capability


@pytest.fixture
def knowledge(back, body_massage, curator, relax):
    """Две разные подтверждённые способности под цель — минимум для плана."""
    _capability(back, "muscle-tension-relief", curator, relax)
    _capability(body_massage, "temporary-relaxation", curator, relax)


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _compose(body: dict | None = None, who: str = OWNER) -> dict:
    resp = _api(who).post(DECISION_URL, body if body is not None else _body(), format="json")
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


# ─── 1. план из подтверждённого знания ───────────────────────────────────────


class TestComposes:
    def test_one_optional_capability_step_per_confirmed_capability(self, goal, knowledge) -> None:
        data = _compose()
        assert data["outcome"] == "PLAN"
        decision = data["decision"]
        steps = decision["steps"]
        assert [s["capability_ref"] for s in steps] == ["muscle-tension-relief", "temporary-relaxation"]
        for step in steps:
            assert set(step) == STEP_KEYS
            assert (step["role"], step["level"]) == ("OPTIONAL", "CAPABILITY")
            assert step["canonical_service_ref"] is None and step["tenant_offer_ref"] is None
            assert step["alternative_of"] is None and step["execution"] is None
            assert step["outcome_ref"] == goal.goal_key
        assert decision["goal_ref"] == str(goal.id)
        assert decision["justification"] == "JUSTIFIED"
        assert decision["safety_state"] == "NORMAL"

    def test_the_plan_is_incomplete_and_says_why_per_step(self, goal, knowledge) -> None:
        decision = _compose()["decision"]
        steps = decision["steps"]
        assert decision["validation"]["status"] == "INCOMPLETE"
        assert decision["validation"]["step_validations"] == {s["step_id"]: "INCOMPLETE" for s in steps}
        by_id = {a["assertion_id"]: a for a in decision["assertions"]}
        for step in steps:
            kinds = sorted(by_id[a]["kind"] for a in step["assertions"])
            # Правило с субъектом «канон» к шагу уровня способности не применимо.
            assert kinds == ["CAPABILITY_SEMANTICS", "SERVICE_CAPABILITY_MAPPING"]
            for assertion_id in step["assertions"]:
                a = by_id[assertion_id]
                assert a["subject"] == {"capability_id": step["capability_ref"]}
                assert a["value"] == {
                    "state": "Unknown", "reason": "NO_RULE_EXISTS", "asked_source": SOURCE, "as_of": "2026-09-08",
                }
                assert set(a["provenance"]) == {"rule_id", "source", "version", "applicability"}
                assert a["provenance"]["rule_id"] and a["provenance"]["version"] == "0.1"

    def test_known_rules_make_the_step_valid_but_the_plan_stays_incomplete(self, goal, knowledge) -> None:
        """§12: без знания об обязательности (ни одного CORE) план INCOMPLETE,
        даже когда каждый шаг VALID."""
        body = _body()
        for rule in body["rules_registry"]["rules"][:2]:
            rule.update(status="KNOWN", value={"mapped": True})
        decision = _compose(body)["decision"]
        assert set(decision["validation"]["step_validations"].values()) == {"VALID"}
        assert decision["validation"]["status"] == "INCOMPLETE"
        assert {a["value"]["state"] for a in decision["assertions"]} == {"Known"}

    def test_a_rule_naming_one_capability_applies_to_that_step_only(self, goal, knowledge) -> None:
        body = _body()
        body["rules_registry"]["rules"].append(
            _rule(
                "CAPABILITY_SEMANTICS", "capability", rule_id="PR-CAPABILITY_SEMANTICS-0002",
                applicability={"scope": "GENERAL", "subject_kind": "capability",
                               "subject_ids": ["temporary-relaxation"], "conditions": []},
            )
        )
        steps = {s["capability_ref"]: s for s in _compose(body)["decision"]["steps"]}
        assert len(steps["temporary-relaxation"]["assertions"]) == 3
        assert len(steps["muscle-tension-relief"]["assertions"]) == 2

    def test_an_intentionally_unsupported_rule_is_unknown_by_policy(self, goal, knowledge) -> None:
        body = _body()
        body["rules_registry"]["rules"][0].update(status="INTENTIONALLY_UNSUPPORTED", value=None)
        decision = _compose(body)["decision"]
        withheld = [a for a in decision["assertions"] if a["kind"] == "CAPABILITY_SEMANTICS"]
        assert withheld and {a["value"]["reason"] for a in withheld} == {"POLICY_WITHHELD"}
        assert set(decision["validation"]["step_validations"].values()) == {"INCOMPLETE"}

    def test_the_six_policy_versions_name_their_sources(self, goal, knowledge) -> None:
        from recommendation.api import REASON_CODE_REGISTRY_VERSION, RESOLVER_SPEC_VERSION

        assert _compose()["decision"]["policy_versions"] == {
            "plan_spec_version": "1.0",
            "constraint_policy_version": "0.1",
            "resolver_spec_version": RESOLVER_SPEC_VERSION,
            "catalog_mapping_version": "unversioned",
            "safety_policy_version": "safety-digest-1",
            "reason_code_registry_version": REASON_CODE_REGISTRY_VERSION,
        }

    def test_the_same_input_gives_the_same_plan_apart_from_ids(self, goal, knowledge) -> None:
        def shape(decision: dict) -> list:
            return [(s["capability_ref"], s["role"], s["level"], len(s["assertions"])) for s in decision["steps"]]

        first, second = _compose()["decision"], _compose()["decision"]
        assert shape(first) == shape(second)
        assert first["decision_id"] != second["decision_id"]
        # step_id не переиспользуется между решениями (§9.1).
        assert not {s["step_id"] for s in first["steps"]} & {s["step_id"] for s in second["steps"]}

    def test_one_capability_on_two_procedures_is_one_step(self, goal, back, body_massage, curator, relax) -> None:
        # Одна запись словаря у двух процедур — один шаг: свойство схемы (DRF-2743).
        _capability([back, body_massage], "temporary-relaxation", curator, relax)
        _capability(back, "muscle-tension-relief", curator, relax)
        refs = [s["capability_ref"] for s in _compose()["decision"]["steps"]]
        assert refs == ["muscle-tension-relief", "temporary-relaxation"]

    def test_nothing_is_saved(self, goal, knowledge) -> None:
        _compose()
        assert not Plan.objects.exists() and not PlanRevision.objects.exists()

    def test_the_course_and_horizon_on_the_link_never_reach_the_plan(
        self, goal, back, body_massage, curator, relax,
    ) -> None:
        note = {"course_pattern": "обычно рассматривается как курс сеансов",
                "result_horizon": "через несколько недель",
                "variability_note": "зависит от исходного состояния",
                "evidence_source": "обзор владельца 2871"}
        _capability(back, "muscle-tension-relief", curator, relax, link=note)
        _capability(body_massage, "temporary-relaxation", curator, relax, link=note)
        assert CapabilityGoalLink.objects.exclude(course_pattern="").count() == 2  # в базе они есть
        data = _compose()
        flat = repr(data)
        assert "курс" not in flat and "недель" not in flat
        found = set(_keys(data))
        assert "steps" in found
        assert not found & PLAN_FORBIDDEN_FIELDS
        assert not {"course_pattern", "result_horizon"} & found


# ─── 2. решение сохраняется командой WP1 без переделки ───────────────────────


class TestTheDecisionSavesAsIs:
    def test_compose_then_save_through_the_real_endpoint(self, goal, knowledge) -> None:
        decision = _compose()["decision"]
        command = {
            "decision_id": decision["decision_id"],
            "goal_ref": decision["goal_ref"],
            "mode": "SAVE",
            "confirmation": {"question_id": "plan.save", "option_id": "yes", "state_revision": 3},
            "safety_state": "NORMAL", "safety_policy_version": "pre_check-test", "evaluated_at_revision": 4,
            "provenance": {"policy_versions": decision["policy_versions"]},
            "decision": {k: decision[k] for k in ("steps", "assertions", "validation")},
        }
        resp = _api().post(PLAN_URL, command, format="json")
        assert resp.status_code == 201, resp.content
        revision = PlanRevision.objects.get()
        assert revision.steps_snapshot == decision["steps"]
        assert revision.assertions_snapshot == decision["assertions"]
        assert revision.validation == decision["validation"]
        assert revision.created_from == {"decision_id": decision["decision_id"]}

    def test_partial_acceptance_is_a_recomposition_without_the_removed_capability(
        self, goal, back, body_massage, curator, relax,
    ) -> None:
        for key in ("a-first", "b-second", "c-third"):
            _capability(back, key, curator, relax)
        data = _compose(_body(excluded_capability_refs=["b-second"]))
        assert [s["capability_ref"] for s in data["decision"]["steps"]] == ["a-first", "c-third"]

    def test_removing_down_to_one_capability_is_no_longer_a_plan(self, goal, knowledge) -> None:
        data = _compose(_body(excluded_capability_refs=["temporary-relaxation"]))
        assert (data["outcome"], data["decision"]) == ("PLAN_NOT_JUSTIFIED", None)


# ─── 3. что шагом не становится ──────────────────────────────────────────────


class TestOnlyConfirmedKnowledge:
    """Каждый узел: одна способность годна, вторая — нет; план не оправдан,
    значит негодная шагом не стала. Контроль присутствия — ``knowledge``."""

    def _only_one_step_source(self) -> None:
        data = _compose()
        assert (data["outcome"], data["decision"]) == ("PLAN_NOT_JUSTIFIED", None)

    @pytest.mark.parametrize(
        "override",
        [
            {"status": ClaimEvidence.Status.SYSTEM_INFERENCE, "confirmed_by": None, "confirmed_at": None},
            {"claim_scope": ClaimEvidence.ClaimScope.NOT_SUPPORTED},
            {"valid_until": "PAST"},
        ],
    )
    @pytest.mark.parametrize("where", ["cap", "link"])
    def test_an_unconfirmed_unsupported_or_expired_claim(self, goal, back, curator, relax, override, where) -> None:
        override = {k: (timezone.now() - timedelta(days=1) if v == "PAST" else v) for k, v in override.items()}
        _capability(back, "good", curator, relax)
        _capability(back, "bad", curator, relax, **{where: override})
        self._only_one_step_source()

    def test_a_capability_with_no_link_to_the_goal(self, goal, back, curator, relax) -> None:
        _capability(back, "good", curator, relax)
        _capability(back, "unlinked", curator, relax, with_link=False)
        self._only_one_step_source()

    def test_a_capability_linked_to_another_goal(self, goal, back, curator, relax) -> None:
        other = GoalOption.objects.create(key="tone-2871", label="Тонус")
        _capability(back, "good", curator, relax)
        _capability(back, "for-another-goal", curator, other)
        self._only_one_step_source()

    def test_the_control_two_confirmed_capabilities_are_a_plan(self, goal, knowledge) -> None:
        assert _compose()["outcome"] == "PLAN"


# ─── 4. исходы без плана — штатные ответы ────────────────────────────────────


class TestNoPlanOutcomes:
    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_blocking_safety_builds_nothing(self, goal, knowledge, state) -> None:
        data = _compose(_body(safety_state=state))
        assert (data["outcome"], data["decision"], data["safety_state"]) == ("SAFETY_BLOCKED", None, state)

    def test_clarify_builds_nothing_and_says_a_question_is_pending(self, goal, knowledge) -> None:
        """DRF-2877: причина о человеке — область «весь план». Решения,
        собранного при незакрытом вопросе, не существует."""
        data = _compose(_body(safety_state="CLARIFY"))
        assert (data["outcome"], data["decision"], data["safety_state"]) == ("CLARIFY_PENDING", None, "CLARIFY")
        assert data["details"] == {"cause": "SAFETY_CLARIFY"}

    @pytest.mark.parametrize("state", ["NORMAL", "CAUTION"])
    def test_non_blocking_safety_is_carried_on_the_decision(self, goal, knowledge, state) -> None:
        data = _compose(_body(safety_state=state))
        assert data["outcome"] == "PLAN" and data["decision"]["safety_state"] == state

    def test_no_active_goal(self, owner, knowledge) -> None:
        data = _compose()
        assert (data["outcome"], data["details"]) == ("NO_GOAL", {"reason": "no_active_goal"})

    @pytest.mark.parametrize("state", ["paused", "achieved", "archived", "superseded"])
    def test_a_goal_that_is_not_active(self, goal, knowledge, state) -> None:
        ClientGoal.objects.filter(pk=goal.pk).update(state=state)
        assert _compose()["outcome"] == "NO_GOAL"

    def test_a_free_text_goal_has_no_curated_decomposition(self, owner, knowledge) -> None:
        ClientGoal.objects.create(client=owner, goal_text="хочу отдохнуть", source_channel="bot")
        data = _compose()
        assert (data["outcome"], data["details"]) == ("NO_GOAL", {"reason": "goal_has_no_key"})

    def test_no_confirmed_capabilities_is_an_honest_no_plan(self, goal) -> None:
        """Состояние стенда 07.10: мэппинг есть, знания нет."""
        data = _compose()
        assert (data["outcome"], data["decision"]) == ("NO_CURATED_DECOMPOSITION", None)
        assert data["details"] == {"goal_key": goal.goal_key}

    def test_an_inactive_goal_option_is_no_decomposition(self, goal, knowledge, relax) -> None:
        GoalOption.objects.filter(pk=relax.pk).update(is_active=False)
        assert _compose()["outcome"] == "NO_CURATED_DECOMPOSITION"

    def test_one_capability_is_not_a_plan(self, goal, back, curator, relax) -> None:
        _capability(back, "only-one", curator, relax)
        data = _compose()
        assert (data["outcome"], data["decision"]) == ("PLAN_NOT_JUSTIFIED", None)

    def test_a_strangers_goal_is_not_mine(self, goal, knowledge) -> None:
        _user(STRANGER, "+79995028712", is_proxy=True)
        assert _compose(who=STRANGER)["outcome"] == "NO_GOAL"

    def test_engine_off_is_404(self, goal, knowledge, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        resp = _api().post(DECISION_URL, _body(), format="json")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"


# ─── 5. вход: форма контракта ────────────────────────────────────────────────


def _mutated(path: list, value) -> dict:
    body = _body()
    node = body
    for key in path[:-1]:
        node = node[key]
    if value is ...:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return body


RULE0 = ["rules_registry", "rules", 0]


class TestInputContract:
    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (_mutated(["safety_state"], "NOT_APPLICABLE"), "safety_state_invalid"),
            (_mutated(["safety_state"], ...), "safety_state_invalid"),
            (_mutated(["safety_policy_version"], ""), "safety_policy_version_missing"),
            (_mutated(["rules_registry"], ...), "rules_registry_missing"),
            (_mutated(["rules_registry", "registry_version"], ""), "rules_registry_missing"),
            (_mutated(["rules_registry", "rules"], "all"), "rules_registry_missing"),
            (_mutated([*RULE0], "rule"), "rule_not_object"),
            (_mutated([*RULE0, "rule_id"], ""), "rule_id_missing"),
            (_mutated([*RULE0, "kind"], "PLAN_CADENCE"), "rule_kind_unknown"),
            (_mutated([*RULE0, "status"], "MAYBE"), "rule_status_unknown"),
            (_mutated([*RULE0, "provenance"], ...), "rule_provenance_missing"),
            (_mutated([*RULE0, "provenance", "source"], ""), "rule_provenance_missing"),
            (_mutated([*RULE0, "provenance", "version"], ...), "rule_provenance_missing"),
            (_mutated([*RULE0, "applicability"], ...), "rule_applicability_missing"),
            (_mutated([*RULE0, "applicability", "subject_kind"], ""), "rule_applicability_missing"),
            (_mutated([*RULE0, "value"], ...), "rule_unknown_value_malformed"),
            (_mutated([*RULE0, "value", "reason"], "BECAUSE"), "rule_unknown_value_malformed"),
            (_mutated([*RULE0, "value", "asked_source"], ""), "rule_unknown_value_malformed"),
            (_mutated([*RULE0, "value", "as_of"], ...), "rule_unknown_value_malformed"),
            (_mutated(["rules_registry", "rules", 1, "rule_id"], "PR-CAPABILITY_SEMANTICS-0001"), "rule_id_duplicate"),
            (_mutated(["rules_registry", "rules"], [_rule("SAFETY_CONSTRAINT", "canonical_service")]),
             "rules_registry_incomplete"),
            (_mutated(["rules_registry", "rules"], [_rule("CAPABILITY_SEMANTICS", "capability")]),
             "rules_registry_incomplete"),
            (_mutated(["excluded_capability_refs"], "one"), "excluded_capability_refs_malformed"),
            (_mutated(["excluded_capability_refs"], [""]), "excluded_capability_refs_malformed"),
        ],
    )
    def test_each_refusal_by_name(self, goal, knowledge, body, reason) -> None:
        resp = _api().post(DECISION_URL, copy.deepcopy(body), format="json")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["code"] == "PLAN_CONTRACT_VIOLATION"
        assert resp.json()["error"]["details"]["reason"] == reason

    def test_a_known_rule_without_a_value_is_refused(self, goal, knowledge) -> None:
        body = _body()
        body["rules_registry"]["rules"][0].update(status="KNOWN", value=None)
        resp = _api().post(DECISION_URL, body, format="json")
        assert resp.json()["error"]["details"]["reason"] == "rule_known_value_missing"

    def test_the_whole_request_is_refused_not_just_the_bad_rule(self, goal, knowledge) -> None:
        """Частично конформный вход отбрасывается целиком (§4.1)."""
        body = _body()
        body["rules_registry"]["rules"].append(_rule("DURATION", "canonical_service", provenance={}))
        resp = _api().post(DECISION_URL, body, format="json")
        assert resp.status_code == 400
        assert resp.json()["error"]["details"] == {"reason": "rule_provenance_missing", "detail": "PR-DURATION-0001"}
