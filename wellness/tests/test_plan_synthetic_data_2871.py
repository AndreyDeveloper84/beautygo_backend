# flake8: noqa: F811 — фикстуры импортированы из узлов сборки плана и приходят параметрами
"""План на помеченных синтетических данных (DRF-2871).

Решение владельца 08.10.2026: сквозная проверка Плана идёт на подготовленных
тестовых данных через настоящие компоненты. Механика на синтетике не
доказывает обоснованности — поэтому синтетика нигде не выглядит как
подтверждённое знание. Что держат узлы:

* синтетику читает СЕРВЕР по личности: настройка стенда
  ``SYNTHETIC_TEST_DATA_ENABLED``, субъект в серверном списке
  ``SYNTHETIC_TEST_SUBJECT_IDS`` и признак тестовой персоны. Нет любого — её
  не существует; из тела запроса разрешение прислать нельзя;
* план, собранный с синтетикой, помечен; сохранённый — помечен навсегда,
  и пометку не снять ни моделью, ни ``update()``;
* сохранение само перечитывает знание: шаг обязан стоять на подтверждённом
  знании либо — по серверному разрешению — на помеченной синтетике. Срезанная
  пометка ответа и выключенный обратно флаг запрета не обходят;
* остальное не ослаблено: безопасность хода, оправданность, «не поддержано»,
  истёкшее.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.capabilities import (
    capability_keys_helping_goal,
    procedures_by_capability_helping_goal,
    synthetic_capability_keys_helping_goal,
)
from services.models import CapabilityGoalLink, ClaimEvidence, ServiceTemplate
from services.synthetic import grant_for
from users.models import User
from wellness.models import Plan, PlanRevision
from wellness.tests.test_plan_compose_2871 import (  # noqa: F401 — фикстуры того же сценария
    DECISION_URL,
    PLAN_URL,
    _api,
    _body,
    _capability,
    _token_and_flag,
    back,
    body_massage,
    category,
    curator,
    goal,
    knowledge,
    owner,
    relax,
)

pytestmark = pytest.mark.django_db

#: Помеченное синтетическое утверждение: вывод системы, никем не подтверждён.
SYNTHETIC = {
    "status": ClaimEvidence.Status.SYSTEM_INFERENCE, "confirmed_by": None, "confirmed_at": None, "synthetic": True,
}
#: То же без пометки — обычное неподтверждённое знание. Его не читает никто.
UNCONFIRMED = {"status": ClaimEvidence.Status.SYSTEM_INFERENCE, "confirmed_by": None, "confirmed_at": None}


def _allow(settings, user, *, flag=True, listed=True, persona=True) -> None:
    """Три условия серверного разрешения — порознь, чтобы снять любое одно."""
    settings.SYNTHETIC_TEST_DATA_ENABLED = flag
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(user.pk)] if listed else []
    User.objects.filter(pk=user.pk).update(is_test_persona=persona)


@pytest.fixture
def test_data_on(settings, owner):
    _allow(settings, owner)


@pytest.fixture
def synthetic_canons(category):
    """Синтетическое знание привязывается только к синтетическому канону."""
    return [
        ServiceTemplate.objects.create(category=category, name=f"Синтетическая процедура {n} 2871", synthetic=True)
        for n in ("А", "Б")
    ]


@pytest.fixture
def synthetic(synthetic_canons, curator, relax):
    """Две помеченные способности под цель, у разных процедур."""
    for canon, key in zip(synthetic_canons, ("synthetic-a", "synthetic-b")):
        _capability(canon, key, curator, relax, cap=SYNTHETIC, link=SYNTHETIC)


def _compose(body: dict | None = None, **over) -> dict:
    resp = _api().post(DECISION_URL, body if body is not None else _body(**over), format="json")
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _save_command(decision: dict, **over) -> dict:
    return {
        "decision_id": decision["decision_id"],
        "goal_ref": decision["goal_ref"],
        "confirmation": {"question_id": "plan.save", "option_id": "yes", "state_revision": 1},
        "safety_state": "NORMAL", "safety_policy_version": "pre_check-test", "evaluated_at_revision": 4,
        "provenance": {"policy_versions": decision["policy_versions"]},
        "decision": {k: decision[k] for k in ("steps", "assertions", "validation")},
        **over,
    }


def _save(decision: dict, **over):
    return _api().post(PLAN_URL, _save_command(decision, **over), format="json")


MISSING_ONE = [{"flag": False}, {"listed": False}, {"persona": False}]
#: Вне серверного списка при включённых тестовых данных движка нет вовсе
#: (вторая линия изоляции, владелец 10.10): не «плана нет», а «выключено».
OFF_THE_LIST = {"listed": False}


def _engine_is_off(resp) -> None:
    assert resp.status_code == 404, resp.content
    assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"


class TestTheServerDecidesWhoReadsSynthetic:
    """Синтетика существует только для субъекта, которому сервер её разрешил."""

    @pytest.mark.parametrize("missing", MISSING_ONE)
    def test_without_any_one_condition_synthetic_knowledge_gives_no_plan(
        self, goal, synthetic, settings, owner, missing,
    ) -> None:
        _allow(settings, owner, **missing)
        if missing == OFF_THE_LIST:
            _engine_is_off(_api().post(DECISION_URL, _body(), format="json"))
            return
        data = _compose()
        assert (data["outcome"], data["decision"]) == ("NO_CURATED_DECOMPOSITION", None)

    @pytest.mark.parametrize("value", [True, "true", 1, {"subject_id": "x"}])
    def test_the_request_cannot_ask_for_synthetic(self, goal, synthetic, settings, owner, value) -> None:
        """Разрешение — не поле запроса: присланное в теле ничего не включает,
        даже при включённом стенде и субъекте в списке, если сервер разрешения
        на синтетику не выдал (нет признака тестовой персоны)."""
        _allow(settings, owner, persona=False)
        data = _compose(include_synthetic=value)
        assert (data["outcome"], data["decision"]) == ("NO_CURATED_DECOMPOSITION", None)

    def test_another_persons_permission_does_not_carry_over(self, goal, synthetic, settings, owner) -> None:
        """В списке стоит другой субъект — у этого нет ни синтетики, ни движка."""
        other = User.objects.create_user(
            username="plan_synth_other", password="x", role="client", phone="+79995028771", is_test_persona=True,
        )
        _allow(settings, owner)
        settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(other.pk)]
        _engine_is_off(_api().post(DECISION_URL, _body(), format="json"))

    def test_under_the_permission_the_plan_is_composed_and_marked(self, goal, synthetic, test_data_on) -> None:
        data = _compose()
        assert data["outcome"] == "PLAN"
        assert data["synthetic"] is True
        assert data["synthetic_capability_refs"] == ["synthetic-a", "synthetic-b"]
        assert [s["capability_ref"] for s in data["decision"]["steps"]] == ["synthetic-a", "synthetic-b"]

    def test_a_plan_on_confirmed_knowledge_is_not_marked_even_under_the_permission(
        self, goal, knowledge, test_data_on,
    ) -> None:
        data = _compose()
        assert data["outcome"] == "PLAN"
        assert (data["synthetic"], data["synthetic_capability_refs"]) == (False, [])

    def test_one_synthetic_step_marks_the_whole_plan(self, goal, knowledge, synthetic_canons, curator, relax, test_data_on) -> None:
        _capability(synthetic_canons[0], "synthetic-a", curator, relax, cap=SYNTHETIC, link=SYNTHETIC)
        data = _compose()
        assert len(data["decision"]["steps"]) == 3
        assert (data["synthetic"], data["synthetic_capability_refs"]) == (True, ["synthetic-a"])

    def test_unmarked_unconfirmed_knowledge_is_read_by_no_one(self, goal, back, body_massage, curator, relax, test_data_on) -> None:
        """Под флагом читается ПОМЕЧЕННОЕ, а не «любое неподтверждённое»."""
        _capability(back, "inferred-a", curator, relax, cap=UNCONFIRMED, link=UNCONFIRMED)
        _capability(body_massage, "inferred-b", curator, relax, cap=UNCONFIRMED, link=UNCONFIRMED)
        assert _compose()["outcome"] == "NO_CURATED_DECOMPOSITION"

    def test_the_readers_agree_with_the_permission(self, goal, synthetic, relax, settings, owner) -> None:
        assert capability_keys_helping_goal(relax.key) == ()
        assert procedures_by_capability_helping_goal(relax.key) == {}
        assert synthetic_capability_keys_helping_goal(relax.key) == frozenset()
        _allow(settings, owner)
        grant = grant_for(User.objects.get(pk=owner.pk))
        assert sorted(procedures_by_capability_helping_goal(relax.key, include_synthetic=grant)) == [
            "synthetic-a", "synthetic-b",
        ]
        assert synthetic_capability_keys_helping_goal(relax.key, include_synthetic=grant) == {
            "synthetic-a", "synthetic-b",
        }
        # Разрешение перепроверяется при каждом чтении: стенд выключили — тот же
        # объект больше ничего не открывает.
        settings.SYNTHETIC_TEST_DATA_ENABLED = False
        assert procedures_by_capability_helping_goal(relax.key, include_synthetic=grant) == {}

    @pytest.mark.parametrize("not_a_grant", [True, False, 1, "yes"])
    def test_the_readers_refuse_anything_that_is_not_a_grant(self, relax, not_a_grant) -> None:
        with pytest.raises(TypeError):
            procedures_by_capability_helping_goal(relax.key, include_synthetic=not_a_grant)
        with pytest.raises(TypeError):
            synthetic_capability_keys_helping_goal(relax.key, include_synthetic=not_a_grant)


class TestLabelsOfSyntheticSteps:
    """У шага текста нет — подпись берётся отдельной ручкой. Синтетическая
    подпись читается по тому же разрешению и приходит с пометкой."""

    LABELS_URL = f"{PLAN_URL}capability-labels/"

    def _labels(self, *keys: str) -> dict:
        resp = _api().post(self.LABELS_URL, {"keys": list(keys)}, format="json")
        assert resp.status_code == 200, resp.content
        return resp.json()["data"]["labels"]

    def test_under_the_permission_a_synthetic_label_is_given_and_marked(self, goal, synthetic, test_data_on) -> None:
        assert self._labels("synthetic-a")["synthetic-a"] == {
            "state": "labelled", "label": "Помогает: synthetic-a", "expected_effect": None, "synthetic": True,
        }

    @pytest.mark.parametrize("missing", MISSING_ONE)
    def test_without_the_permission_there_is_no_such_label(self, goal, synthetic, settings, owner, missing) -> None:
        _allow(settings, owner, **missing)
        if missing == OFF_THE_LIST:
            _engine_is_off(_api().post(self.LABELS_URL, {"keys": ["synthetic-a"]}, format="json"))
            return
        label = self._labels("synthetic-a")["synthetic-a"]
        assert (label["label"], label["synthetic"]) == (None, False)
        assert label["state"] != "labelled"

    def test_a_confirmed_label_is_never_marked(self, goal, knowledge, test_data_on) -> None:
        label = self._labels("muscle-tension-relief")["muscle-tension-relief"]
        assert (label["state"], label["synthetic"]) == ("labelled", False)


class TestNothingElseIsRelaxed:
    @pytest.fixture(autouse=True)
    def _on(self, test_data_on):
        pass

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_safety(self, goal, synthetic, state) -> None:
        data = _compose(_body(safety_state=state))
        assert (data["outcome"], data["decision"]) == ("SAFETY_BLOCKED", None)

    def test_one_synthetic_capability_is_still_not_a_plan(self, goal, synthetic_canons, curator, relax) -> None:
        _capability(synthetic_canons[0], "synthetic-a", curator, relax, cap=SYNTHETIC, link=SYNTHETIC)
        assert _compose()["outcome"] == "PLAN_NOT_JUSTIFIED"

    def test_two_synthetic_capabilities_of_one_procedure_are_one_visit(self, goal, synthetic_canons, curator, relax) -> None:
        for key in ("synthetic-a", "synthetic-b"):
            _capability(synthetic_canons[0], key, curator, relax, cap=SYNTHETIC, link=SYNTHETIC)
        data = _compose()
        assert (data["outcome"], data["details"]["reason"]) == ("PLAN_NOT_JUSTIFIED", "one_procedure_covers_all")

    @pytest.mark.parametrize(
        "override", [{"claim_scope": ClaimEvidence.ClaimScope.NOT_SUPPORTED}, {"valid_until": "PAST"}],
    )
    @pytest.mark.parametrize("where", ["cap", "link"])
    def test_unsupported_and_expired_synthetic_claims_stay_out(
        self, goal, synthetic_canons, curator, relax, override, where,
    ) -> None:
        override = {k: (timezone.now() - timedelta(days=1) if v == "PAST" else v) for k, v in override.items()}
        _capability(synthetic_canons[0], "synthetic-a", curator, relax, cap=SYNTHETIC, link=SYNTHETIC)
        bad = {"cap": dict(SYNTHETIC), "link": dict(SYNTHETIC)}
        bad[where].update(override)
        _capability(synthetic_canons[1], "synthetic-bad", curator, relax, **bad)
        assert _compose()["outcome"] == "PLAN_NOT_JUSTIFIED"


class TestSavingASyntheticPlan:
    """Действие приёмки 4: «сохранить, закрыть и снова открыть» — на тестовых данных."""

    def test_saved_under_the_permission_marked_and_read_back_marked(self, goal, synthetic, test_data_on) -> None:
        decision = _compose()["decision"]
        saved = _save(decision)
        assert saved.status_code == 201, saved.content
        assert saved.json()["data"]["plan"]["synthetic"] is True
        assert Plan.objects.get().synthetic is True
        reopened = _api().get(PLAN_URL).json()["data"]["plan"]
        assert reopened["synthetic"] is True
        assert [s["capability_ref"] for s in reopened["revision"]["steps"]] == ["synthetic-a", "synthetic-b"]

    def test_a_repeat_of_the_save_is_the_same_plan(self, goal, synthetic, test_data_on) -> None:
        decision = _compose()["decision"]
        command = _save_command(decision)
        first = _api().post(PLAN_URL, command, format="json")
        again = _api().post(PLAN_URL, command, format="json")
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json()["data"]["created"] is False
        assert (Plan.objects.count(), PlanRevision.objects.count()) == (1, 1)

    @pytest.mark.parametrize("missing", MISSING_ONE)
    def test_a_condition_withdrawn_before_saving_refuses_the_save(
        self, goal, synthetic, settings, owner, missing,
    ) -> None:
        """Собрано под разрешением; до сохранения субъекта убрали из списка,
        сняли признак персоны или выключили стенд — сохранение перечитывает
        знание само и отказывает. Присланное в команде поле не помогает."""
        _allow(settings, owner)
        decision = _compose()["decision"]
        _allow(settings, owner, **missing)
        resp = _save(decision, include_synthetic=True)
        if missing == OFF_THE_LIST:
            _engine_is_off(resp)
            assert not Plan.objects.exists() and not PlanRevision.objects.exists()
            return
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_CAPABILITY_NOT_CONFIRMED"
        assert resp.json()["error"]["details"] == {"capability_ref": "synthetic-a"}
        assert not Plan.objects.exists() and not PlanRevision.objects.exists()

    def test_a_plan_on_confirmed_knowledge_is_saved_unmarked_under_the_permission(
        self, goal, knowledge, test_data_on,
    ) -> None:
        decision = _compose()["decision"]
        saved = _save(decision)
        assert saved.status_code == 201
        assert saved.json()["data"]["plan"]["synthetic"] is False

    def test_one_synthetic_step_marks_the_saved_plan(self, goal, knowledge, synthetic_canons, curator, relax, test_data_on) -> None:
        _capability(synthetic_canons[0], "synthetic-a", curator, relax, cap=SYNTHETIC, link=SYNTHETIC)
        saved = _save(_compose()["decision"])
        assert saved.status_code == 201
        assert Plan.objects.get().synthetic is True


class TestSavingReadsTheKnowledgeItself:
    """Запрет стоит на знании, а не на пометке ответа сборки."""

    def test_a_hand_made_command_on_an_invented_capability_is_refused(self, goal, knowledge) -> None:
        decision = _compose()["decision"]
        decision["steps"][0]["capability_ref"] = "invented-capability"
        resp = _save(decision)
        assert resp.status_code == 409
        assert resp.json()["error"]["details"] == {"capability_ref": "invented-capability"}
        assert not Plan.objects.exists()

    def test_an_invented_capability_is_refused_under_the_permission_too(self, goal, synthetic, test_data_on) -> None:
        decision = _compose()["decision"]
        decision["steps"][0]["capability_ref"] = "invented-capability"
        resp = _save(decision)
        assert resp.status_code == 409
        assert resp.json()["error"]["details"] == {"capability_ref": "invented-capability"}

    def test_knowledge_withdrawn_between_composing_and_saving_refuses(self, goal, knowledge, relax) -> None:
        decision = _compose()["decision"]
        link = CapabilityGoalLink.objects.filter(goal=relax).first()
        CapabilityGoalLink.objects.filter(pk=link.pk).update(valid_until=timezone.now() - timedelta(minutes=1))
        resp = _save(decision)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_CAPABILITY_NOT_CONFIRMED"

    def test_a_repeat_of_an_already_saved_command_is_not_judged_again(self, goal, knowledge, relax) -> None:
        command = _save_command(_compose()["decision"])
        assert _api().post(PLAN_URL, command, format="json").status_code == 201
        CapabilityGoalLink.objects.filter(goal=relax).update(valid_until=timezone.now() - timedelta(minutes=1))
        again = _api().post(PLAN_URL, command, format="json")
        assert again.status_code == 200 and again.json()["data"]["created"] is False

    def test_an_ordinary_plan_is_saved_as_before(self, goal, knowledge) -> None:
        saved = _save(_compose()["decision"])
        assert saved.status_code == 201
        assert saved.json()["data"]["plan"]["synthetic"] is False


class TestTheMarkOnThePlanNeverChanges:
    @pytest.fixture
    def marked(self, goal, synthetic, test_data_on) -> Plan:
        _save(_compose()["decision"])
        return Plan.objects.get()

    def test_the_model_cannot_clear_it(self, marked) -> None:
        marked.synthetic = False
        with pytest.raises(IntegrityError, match="synthetic_mark_is_immutable"), transaction.atomic():
            marked.save(update_fields=["synthetic"])

    def test_a_queryset_update_cannot_clear_it(self, marked) -> None:
        with pytest.raises(IntegrityError, match="synthetic_mark_is_immutable"), transaction.atomic():
            Plan.objects.filter(pk=marked.pk).update(synthetic=False)

    def test_an_ordinary_plan_cannot_be_marked_afterwards(self, goal, knowledge) -> None:
        _save(_compose()["decision"])
        with pytest.raises(IntegrityError, match="synthetic_mark_is_immutable"), transaction.atomic():
            Plan.objects.update(synthetic=True)

    def test_the_status_still_changes_and_the_mark_stays(self, marked) -> None:
        resp = _api().post(f"{PLAN_URL}state/", {"plan_id": str(marked.id), "state": "paused"}, format="json")
        assert resp.status_code == 200, resp.content
        marked.refresh_from_db()
        assert (marked.status, marked.synthetic) == ("paused", True)

    def test_the_mark_survives_the_permission_being_withdrawn(self, marked, settings, owner) -> None:
        _allow(settings, owner, persona=False)  # в списке, разрешения на синтетику больше нет
        assert _api().get(PLAN_URL).json()["data"]["plan"]["synthetic"] is True

    def test_taken_off_the_list_the_plan_is_not_read_and_keeps_its_mark(self, marked, settings, owner) -> None:
        _allow(settings, owner, listed=False)
        assert _api().get(PLAN_URL).json()["data"]["plan"] is None
        assert Plan.objects.get().synthetic is True

    def test_the_mark_survives_the_test_data_being_switched_off(self, marked, settings) -> None:
        """Выключили тестовые данные — сохранённый синтетический план не
        пропадает и не становится настоящим: он читается с пометкой."""
        settings.SYNTHETIC_TEST_DATA_ENABLED = False
        assert _api().get(PLAN_URL).json()["data"]["plan"]["synthetic"] is True
