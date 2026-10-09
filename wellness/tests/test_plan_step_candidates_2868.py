# flake8: noqa: F811 — фикстуры импортированы из узлов шагов плана и приходят параметрами
"""Шаг плана → услуга: кандидаты и перепроверка выбора (DRF-2868, контракт §8.2).

Шаг несёт способность. Довести его до услуги салона можно только через
подбор; каталог ищет предложения по способности в видимости ЭТОГО человека,
проверяет допуск (строго: «проверка не действует» — не сошлась) и
происхождение ответа о проверке здоровья.

Допуск здесь НАСТОЯЩИЙ — без подмен: услуга размечена, связь подтверждена,
мастер продаётся. Что держат узлы:

* кандидат — только предложение, прошедшее все три слоя; порядок устойчивый,
  каталог не ранжирует;
* каждая причина пустоты названа отдельно; смесь — отдельным значением;
* неподтверждённый ответ о здоровье (в любую сторону) — «условия услуги не
  определены», не «проходит» и не «нужен расспрос»;
* выбор перепроверяется в момент выбора: предложение, не являющееся
  кандидатом СЕЙЧАС, шаг не принимает — обойти подбор нельзя;
* ручка ничего не пишет и уважает те же гейты, что переход шага.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from recommendation.tests.test_goal_fit_depth_2789 import _signed
from recommendation.tests.test_legal_gate_cat10_ext_2843 import _does, _master, _offer as _admitted_offer
from recommendation.tests.test_synthetic_admission_2916 import _outside_body_care
from services.models import ProcedureCapability, SalonService, ServiceTemplate, SpecialistService
from services.synthetic import SYNTHETIC_RULE
from services.tests.test_synthetic_test_mark import _capability as _synthetic_capability
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from wellness.models import Plan, PlanStepResolution
from wellness.plan_restrictions import CAUSES, Cause
from wellness.tests.test_plan_engine_steps_2868 import (  # noqa: F401 — фикстуры того же сценария
    OWNER,
    PLAN_URL,
    RESOLUTION_URL,
    SAFETY,
    STRANGER,
    _api,
    _save,
    _step,
    _token_and_flag,
    category,
    goal,
    owner,
    stranger,
    tenant,
)

pytestmark = pytest.mark.django_db

CANDIDATES_URL = "/api/v1/internal/me/plan/steps/candidates/"
RESTRICTIONS_URL = "/api/v1/internal/me/plan/restrictions/"
#: Способность шага в помощнике ``_step`` узлов шагов.
KEY = "cap:relaxation-massage"
#: Способность синтетической цепочки — отдельный ключ.
SYNTHETIC_KEY = "synthetic_event_look_2868"
Scope = ServiceTemplate.BodyCareScope
LC = ServiceTemplate.LegalServiceClass


@pytest.fixture
def curator(db) -> User:
    return User.objects.create_user(
        username="plan_step_candidates_curator", password="x", role="client", phone="+79995028690",
    )


def _classified(offering: SalonService, curator: User) -> None:
    """Область и юридический класс канона подтверждены человеком — такая
    услуга не едет ни на одном обходе, при любом положении флага каталога."""
    ServiceTemplate.objects.filter(pk=offering.template_id).update(
        body_care_scope=Scope.NOT_BODY_CARE, scope_confirmed_by=curator,
        scope_confirmed_at=timezone.now(), scope_source_ref="решение владельца",
        legal_service_class=LC.NON_MEDICAL_COSMETIC, legal_class_confirmed_by=curator,
        legal_class_confirmed_at=timezone.now(), legal_class_source_ref="решение юриста",
    )


def _health(offering: SalonService, curator: User, *, required: bool) -> None:
    """Ответ САЛОНА о проверке здоровья, подтверждённый человеком."""
    SalonService.objects.filter(pk=offering.pk).update(
        requires_health_check=required,
        health_check_origin=SalonService.HealthCheckAnswerOrigin.CONFIRMED,
        health_check_confirmed_by=curator,
        health_check_confirmed_at=timezone.now(),
        health_check_source_ref="ответ салона, сверен",
    )


def _can(templates, curator: User, key: str = KEY) -> None:
    ProcedureCapability.objects.create(
        templates=list(templates), key=key, text_client="Помогает расслабиться", **_signed(curator, True),
    )


def _ready_offer(tenant, category, curator, *, name: str, suffix: str, health: bool | None = False):
    """Услуга, готовая стать кандидатом: размечена, с продаваемым мастером;
    ``health`` — подтверждённый ответ салона (``None`` — ответа нет)."""
    master = _master(tenant, suffix)
    offering = _admitted_offer(tenant, category, curator, name=name)
    _does(master, offering)
    _classified(offering, curator)
    if health is not None:
        _health(offering, curator, required=health)
    return master, offering


@pytest.fixture
def massage(tenant, category, curator):
    master, offering = _ready_offer(tenant, category, curator, name="Массаж расслабляющий", suffix="61")
    _can([offering.template], curator)
    return master, offering


def _candidates(plan: Plan, step_id: str = "s1", who: str = OWNER, **over):
    return _api(who).post(CANDIDATES_URL, {"plan_id": str(plan.id), "step_id": step_id, **SAFETY, **over}, format="json")


def _found(plan: Plan, step_id: str = "s1") -> dict:
    resp = _candidates(plan, step_id)
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _choose(plan: Plan, offering: SalonService, search_id: str, step_id: str = "s1", **over):
    body = {
        "plan_id": str(plan.id), "step_id": step_id, "level": "OFFER",
        "canonical_service_ref": str(offering.template_id), "tenant_offer_ref": str(offering.pk),
        "resolver_decision_id": search_id, **SAFETY, **over,
    }
    return _api().post(RESOLUTION_URL, body, format="json")


class TestACandidate:
    def test_an_admitted_offer_with_a_confirmed_health_answer_is_a_candidate(self, goal, massage) -> None:
        master, offering = massage
        data = _found(_save(goal))
        assert (data["step_id"], data["capability_ref"], data["nothing_because"]) == ("s1", KEY, None)
        assert data["candidates"] == [
            {
                "tenant_offer_ref": str(offering.pk),
                "canonical_service_ref": str(offering.template_id),
                "health_check": "not_required",
                "display": {
                    "service_name": "Массаж расслабляющий",
                    "salon": {"name": offering.tenant.name, "city": None, "address": None},
                    "masters": [
                        {
                            "specialist_ref": str(master.pk), "name": "Мастер 61",
                            "price": "2000.00", "duration_minutes": 60,
                        },
                    ],
                },
                "synthetic": False,
            },
        ]
        assert uuid.UUID(data["search_id"])
        assert data["rejected"] == {"NO_SELLABLE_MASTER": 0, "NOT_ADMITTED": 0, "HEALTH_CONDITIONS_UNDEFINED": 0}

    def test_a_confirmed_need_for_a_health_check_is_shown_not_hidden(self, goal, tenant, category, curator) -> None:
        """Подтверждённое «расспрос нужен» — услуга кандидат; расспрос встретит
        запись, как и прежде."""
        _, offering = _ready_offer(tenant, category, curator, name="Массаж глубокий", suffix="62", health=True)
        _can([offering.template], curator)
        [candidate] = _found(_save(goal))["candidates"]
        assert candidate["health_check"] == "required"

    def test_the_salon_address_is_given_in_the_words_of_the_catalog(self, goal, massage) -> None:
        _, offering = massage
        Tenant.objects.filter(pk=offering.tenant_id).update(city="Пенза", address="ул. Московская, 1")
        [candidate] = _found(_save(goal))["candidates"]
        assert candidate["display"]["salon"] == {
            "name": offering.tenant.name, "city": "Пенза", "address": "ул. Московская, 1",
        }

    def test_two_candidates_come_in_a_stable_order_and_are_not_ranked(self, goal, massage, tenant, category, curator) -> None:
        _, first = massage
        _, second = _ready_offer(tenant, category, curator, name="Массаж спины", suffix="63")
        ProcedureCapability.objects.get(key=KEY).templates.add(second.template)
        plan = _save(goal)
        refs = [c["tenant_offer_ref"] for c in _found(plan)["candidates"]]
        assert refs == sorted([str(first.pk), str(second.pk)])
        assert [c["tenant_offer_ref"] for c in _found(plan)["candidates"]] == refs
        assert not any("rank" in c or "score" in c for c in _found(plan)["candidates"])

    def test_only_the_masters_who_pass_are_named(self, goal, massage) -> None:
        """Второй мастер не продаётся — его нет ни в допуске, ни в показе."""
        master, offering = massage
        idle = _master(offering.tenant, "64")
        _does(idle, offering)
        SpecialistProfile.objects.filter(pk=idle.pk).update(is_booking_enabled=False)
        [candidate] = _found(_save(goal))["candidates"]
        assert [m["specialist_ref"] for m in candidate["display"]["masters"]] == [str(master.pk)]

    def test_each_search_has_its_own_id(self, goal, massage) -> None:
        plan = _save(goal)
        assert _found(plan)["search_id"] != _found(plan)["search_id"]

    def test_asking_writes_nothing(self, goal, massage) -> None:
        plan = _save(goal)
        before = _api().get(PLAN_URL).json()["data"]["plan"]
        _found(plan)
        assert _api().get(PLAN_URL).json()["data"]["plan"] == before
        assert not PlanStepResolution.objects.exists()


class TestWhyThereIsNoCandidate:
    """Что мешает подобрать услугу — раздельно (решение владельца 08.10)."""

    def _nothing(self, plan: Plan) -> tuple[str, dict]:
        data = _found(plan)
        assert data["candidates"] == []
        return data["nothing_because"], data["rejected"]

    def test_no_canon_has_the_capability(self, goal) -> None:
        assert self._nothing(_save(goal))[0] == "NO_CAPABILITY"

    def test_the_canon_has_it_but_no_salon_offers_it(self, goal, category, curator) -> None:
        canon = ServiceTemplate.objects.create(category=category, name="Канон без предложений 2868")
        _can([canon], curator)
        assert self._nothing(_save(goal))[0] == "NO_OFFER"

    def test_the_offers_exist_but_not_for_this_person(self, goal, category, curator) -> None:
        demo = Tenant.objects.create(slug="demo-2868", name="Демо-салон", is_active=True, is_demo=True)
        _, offering = _ready_offer(demo, category, curator, name="Массаж в демо", suffix="65")
        _can([offering.template], curator)
        assert self._nothing(_save(goal))[0] == "OUT_OF_SIGHT"

    def test_nobody_sellable_does_it(self, goal, tenant, category, curator) -> None:
        offering = _admitted_offer(tenant, category, curator, name="Массаж без мастера")
        _classified(offering, curator)
        _health(offering, curator, required=False)
        _can([offering.template], curator)
        reason, rejected = self._nothing(_save(goal))
        assert (reason, rejected["NO_SELLABLE_MASTER"]) == ("NO_SELLABLE_MASTER", 1)

    def test_an_offer_whose_scope_and_class_nobody_confirmed_is_not_admitted(
        self, goal, tenant, category, curator,
    ) -> None:
        """Ответ владельца на (c): услуга, у которой проверки «не действуют»,
        в услугу шага не идёт — хотя обычный подбор её показывает."""
        master = _master(tenant, "66")
        offering = _admitted_offer(tenant, category, curator, name="Массаж неразмеченный")
        _does(master, offering)
        _health(offering, curator, required=False)
        _can([offering.template], curator)
        reason, rejected = self._nothing(_save(goal))
        assert (reason, rejected["NOT_ADMITTED"]) == ("NOT_ADMITTED", 1)

    @pytest.mark.parametrize("answer", [None, False, True])
    def test_an_unconfirmed_health_answer_leaves_the_conditions_undefined(
        self, goal, tenant, category, curator, answer,
    ) -> None:
        """Решение владельца 07.10: неподтверждённое «не нужна» — «неизвестно»,
        неподтверждённое «нужна» — «требование не подтверждено», молчание —
        тоже. Ни одно из трёх не открывает шаг."""
        _, offering = _ready_offer(tenant, category, curator, name="Массаж без ответа", suffix="67", health=None)
        SalonService.objects.filter(pk=offering.pk).update(requires_health_check=answer)
        _can([offering.template], curator)
        reason, rejected = self._nothing(_save(goal))
        assert (reason, rejected["HEALTH_CONDITIONS_UNDEFINED"]) == ("HEALTH_CONDITIONS_UNDEFINED", 1)

    def test_a_requirement_raised_by_the_master_alone_is_not_confirmed(self, goal, massage) -> None:
        """У ребра «мастер × услуга» своего происхождения нет: поднятое мастером
        требование — не подтверждено, мастер из кандидатов выпадает."""
        master, offering = massage
        SpecialistService.objects.filter(specialist=master, salon_service=offering).update(requires_health_check=True)
        reason, _ = self._nothing(_save(goal))
        assert reason == "HEALTH_CONDITIONS_UNDEFINED"

    def test_different_reasons_together_are_named_as_a_mix(self, goal, tenant, category, curator) -> None:
        orphan = _admitted_offer(tenant, category, curator, name="Массаж без мастера")
        _classified(orphan, curator)
        _, silent = _ready_offer(tenant, category, curator, name="Массаж без ответа", suffix="68", health=None)
        _can([orphan.template, silent.template], curator)
        reason, rejected = self._nothing(_save(goal))
        assert reason == "NONE_ADMITTED"
        assert rejected == {"NO_SELLABLE_MASTER": 1, "NOT_ADMITTED": 0, "HEALTH_CONDITIONS_UNDEFINED": 1}

    def test_one_good_offer_among_rejected_ones_is_still_a_candidate(self, goal, massage, tenant, category, curator) -> None:
        _, good = massage
        orphan = _admitted_offer(tenant, category, curator, name="Массаж без мастера")
        _classified(orphan, curator)
        ProcedureCapability.objects.get(key=KEY).templates.add(orphan.template)
        data = _found(_save(goal))
        assert [c["tenant_offer_ref"] for c in data["candidates"]] == [str(good.pk)]
        assert (data["nothing_because"], data["rejected"]["NO_SELLABLE_MASTER"]) == (None, 1)


class TestChoosingIsCheckedAgain:
    """§8.2: шаг не получает услугу в обход подбора."""

    def test_a_candidate_is_accepted_and_the_step_reaches_the_offer(self, goal, massage) -> None:
        _, offering = massage
        plan = _save(goal)
        search = _found(plan)["search_id"]
        resp = _choose(plan, offering, search)
        assert resp.status_code == 201, resp.content
        state = resp.json()["data"]["plan"]["step_state"]["s1"]
        assert (state["level"], state["tenant_offer_ref"], state["resolver_decision_id"]) == (
            "OFFER", str(offering.pk), search,
        )

    def test_a_second_tap_on_the_same_choice_is_the_same_row(self, goal, massage) -> None:
        _, offering = massage
        plan = _save(goal)
        search = _found(plan)["search_id"]
        first = _choose(plan, offering, search)
        again = _choose(plan, offering, search, evaluated_at_revision=SAFETY["evaluated_at_revision"] + 3)
        assert (first.status_code, again.status_code) == (201, 200)
        assert PlanStepResolution.objects.count() == 1

    def test_an_offer_of_the_canon_that_is_not_a_candidate_is_refused(self, goal, massage, tenant, category, curator) -> None:
        """Сегодня ручка принимала любое предложение нужного канона. Второе
        предложение того же канона не размечено под показ здоровья — не
        кандидат, и шаг его не принимает."""
        _, offering = massage
        twin = SalonService.objects.create(
            tenant=tenant, name="Массаж-двойник", category=category, template=offering.template,
            duration_minutes=60, is_active=True, base_price=Decimal("3000"),
            mapping_status=SalonService.MappingStatus.VERIFIED, mapping_confirmed_rule="test_fixture",
            mapping_rule_version="1.0.0", mapping_confirmed_at=timezone.now(), mapping_source_ref="fixture:2868",
        )
        _does(_master(tenant, "69"), twin)
        plan = _save(goal)
        search = _found(plan)["search_id"]
        resp = _choose(plan, twin, search)
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_RESOLUTION_REFUSED"
        assert resp.json()["error"]["details"]["reason"] == "offer_not_a_candidate"
        assert not PlanStepResolution.objects.exists()

    def test_a_candidate_that_stopped_being_one_between_showing_and_choosing_is_refused(self, goal, massage) -> None:
        """Перепроверка — в момент выбора, не по памяти показа."""
        master, offering = massage
        plan = _save(goal)
        search = _found(plan)["search_id"]
        SpecialistProfile.objects.filter(pk=master.pk).update(is_booking_enabled=False)
        resp = _choose(plan, offering, search)
        assert resp.json()["error"]["details"]["reason"] == "offer_not_a_candidate"
        assert not PlanStepResolution.objects.exists()

    def test_an_offer_for_another_capability_is_refused(self, goal, massage, tenant, category, curator) -> None:
        _, other = _ready_offer(tenant, category, curator, name="Маникюр", suffix="70")
        _can([other.template], curator, key="cap:something-else")
        plan = _save(goal)
        resp = _choose(plan, other, _found(plan)["search_id"])
        assert resp.json()["error"]["details"]["reason"] == "offer_not_a_candidate"

    def test_the_canon_level_still_needs_no_offer(self, goal, massage) -> None:
        _, offering = massage
        plan = _save(goal)
        resp = _api().post(
            RESOLUTION_URL,
            {"plan_id": str(plan.id), "step_id": "s1", "level": "SERVICE",
             "canonical_service_ref": str(offering.template_id), "resolver_decision_id": "search", **SAFETY},
            format="json",
        )
        assert resp.status_code == 201, resp.content


class TestTheSameGatesAsTheStepItself:
    def _refused(self, resp, reason: str, **details) -> None:
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_NOT_EXECUTABLE"
        assert resp.json()["error"]["details"] == {"reason": reason, **details}

    @pytest.mark.parametrize("state, reason", [("STOP", "safety_blocked"), ("UNKNOWN", "safety_blocked")])
    def test_the_turn_verdict(self, goal, massage, state, reason) -> None:
        self._refused(_candidates(_save(goal), safety_state=state), reason)

    @pytest.mark.parametrize("state", ["CLARIFY", "CAUTION"])
    def test_a_verdict_that_does_not_block_lets_the_search_through(self, goal, massage, state) -> None:
        """Владелец 08.10: универсального вопроса «уточнить» нет — закрывает причина, не вердикт."""
        assert _candidates(_save(goal), safety_state=state).status_code == 200

    def test_an_open_restriction_names_its_question(self, goal, massage, monkeypatch) -> None:
        # Таблица причин каталога пуста до решения владельца — причина подставлена узлом.
        monkeypatch.setitem(CAUSES, "TEST_PLAN_QUESTION", Cause(scopes=frozenset({"PLAN"}), lift_kinds=frozenset()))
        plan = _save(goal)
        opened = _api().post(
            RESTRICTIONS_URL,
            {"plan_id": str(plan.id), "scope": "PLAN", "cause": "TEST_PLAN_QUESTION",
             "question_id": "plan.safety_clarify", **SAFETY},
            format="json",
        )
        assert opened.status_code == 201, opened.content
        self._refused(_candidates(plan), "restriction_open", question_ids=["plan.safety_clarify"])

    def test_a_plan_that_is_not_in_effect(self, goal, massage) -> None:
        plan = _save(goal)
        Plan.objects.filter(pk=plan.pk).update(status=Plan.Status.PAUSED)
        self._refused(_candidates(plan), "plan_not_in_effect")

    def test_a_step_already_at_an_offer_has_nothing_to_search(self, goal, massage) -> None:
        """Исполненный уровень не откатывается (§4.3): сменить услугу шага нельзя."""
        _, offering = massage
        plan = _save(goal)
        assert _choose(plan, offering, _found(plan)["search_id"]).status_code == 201
        self._refused(_candidates(plan), "step_already_resolved")

    def test_a_strangers_plan_and_an_unknown_step_are_not_found(self, goal, massage, stranger) -> None:
        plan = _save(goal)
        theirs = _candidates(plan, who=STRANGER)
        assert (theirs.status_code, theirs.json()["error"]["details"]["reason"]) == (404, "plan_not_found")
        ghost = _candidates(plan, step_id="ghost")
        assert (ghost.status_code, ghost.json()["error"]["details"]["reason"]) == (404, "step_not_found")

    def test_engine_off_404(self, goal, massage, settings) -> None:
        plan = _save(goal)
        settings.PLAN_ENGINE_ENABLED = False
        assert _candidates(plan).json()["error"]["code"] == "PLAN_ENGINE_DISABLED"

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"plan_id": "nope"}, "plan_id_malformed"),
            ({"step_id": ""}, "step_id_missing"),
            ({"safety_state": "NOT_APPLICABLE"}, "safety_state_invalid"),
            ({"evaluated_at_revision": None}, "safety_revision_malformed"),
        ],
    )
    def test_malformed_requests_by_name(self, goal, massage, over, reason) -> None:
        resp = _candidates(_save(goal), **over)
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason


class TestThePersonIsTheViewer:
    def test_a_test_persona_gets_the_offers_of_a_demo_salon(self, goal, owner, category, curator) -> None:
        """Подбор идёт в видимости ЭТОГО человека: демо-салон обычному клиенту
        не виден (узел выше), тестовой личности — виден."""
        demo = Tenant.objects.create(slug="demo-2868-persona", name="Демо-салон", is_active=True, is_demo=True)
        _, offering = _ready_offer(demo, category, curator, name="Массаж в демо", suffix="71")
        _can([offering.template], curator)
        User.objects.filter(pk=owner.pk).update(is_test_persona=True)
        data = _found(_save(goal))
        assert [c["tenant_offer_ref"] for c in data["candidates"]] == [str(offering.pk)]
        assert data["nothing_because"] is None


class TestASyntheticCandidateSaysSo:
    """DRF-2916: синтетическое предложение доходит до кандидата только под
    серверным разрешением этого человека — и несёт пометку; без разрешения
    его не существует."""

    @pytest.fixture
    def synthetic_chain(self, owner, category, curator):
        demo = Tenant.objects.create(slug="demo-2868-synthetic", name="Демо-салон", is_active=True, is_demo=True)
        canon, offering = _outside_body_care(demo, category, curator, "Образ к событию (тест)", synthetic=True)
        _does(_master(demo, "72"), offering)
        # Ответ о проверке здоровья у синтетики подтверждает только синтетическое правило.
        SalonService.objects.filter(pk=offering.pk).update(
            requires_health_check=False,
            health_check_origin=SalonService.HealthCheckAnswerOrigin.CONFIRMED,
            health_check_confirmed_rule=SYNTHETIC_RULE, health_check_rule_version="1",
            health_check_confirmed_at=timezone.now(), health_check_source_ref=SYNTHETIC_RULE,
        )
        # Свой ключ: настоящая способность шага в этих узлах остаётся настоящей.
        _synthetic_capability(canon, SYNTHETIC_KEY, synthetic=True)
        User.objects.filter(pk=owner.pk).update(is_test_persona=True)
        return offering

    def _plan(self, goal) -> Plan:
        return _save(goal, [_step("s1", capability_ref=SYNTHETIC_KEY)])

    def test_under_the_grant_the_candidate_is_marked(self, goal, owner, synthetic_chain, settings) -> None:
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(owner.pk)]
        data = _found(self._plan(goal))
        assert [(c["tenant_offer_ref"], c["synthetic"]) for c in data["candidates"]] == [
            (str(synthetic_chain.pk), True),
        ]

    def test_without_the_grant_there_is_no_such_capability(self, goal, synthetic_chain) -> None:
        data = _found(self._plan(goal))
        assert (data["candidates"], data["nothing_because"]) == ([], "NO_CAPABILITY")


class TestTheDurableRestrictionOfThePerson:
    """Длительное ограничение S1 живёт у бота и переживает разговор. Пока оно
    стоит, действия, ведущие к услуге и записи, закрыты — зарегистрированная
    политика ограничения, которую план раньше не читал вовсе. Сборку,
    сохранение и просмотр оно не закрывает: область — по причине."""

    @pytest.mark.parametrize("state, reason", [("open", "s1_restriction_open"), ("stop", "s1_restriction_stop")])
    def test_candidates_are_closed_under_their_own_names(self, goal, massage, state, reason) -> None:
        resp = _candidates(_save(goal), s1_restriction=state)
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_NOT_EXECUTABLE"
        assert resp.json()["error"]["details"] == {"reason": reason}

    @pytest.mark.parametrize("state, reason", [("open", "s1_restriction_open"), ("stop", "s1_restriction_stop")])
    def test_choosing_an_offer_is_closed_and_nothing_is_written(self, goal, massage, state, reason) -> None:
        _, offering = massage
        plan = _save(goal)
        search = _found(plan)["search_id"]
        resp = _choose(plan, offering, search, s1_restriction=state)
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["details"] == {"reason": reason}
        assert not PlanStepResolution.objects.exists()

    @pytest.mark.parametrize("value", [None, "", "NONE", "cleared", True])
    def test_silence_about_the_restriction_is_not_its_absence(self, goal, massage, value) -> None:
        body = {"plan_id": str(_save(goal).id), "step_id": "s1", **SAFETY}
        if value is None:
            body.pop("s1_restriction")
        else:
            body["s1_restriction"] = value
        resp = _api().post(CANDIDATES_URL, body, format="json")
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["details"]["reason"] == "s1_restriction_invalid"

    def test_a_blocking_turn_verdict_is_named_first(self, goal, massage) -> None:
        resp = _candidates(_save(goal), safety_state="STOP", s1_restriction="stop")
        assert resp.json()["error"]["details"] == {"reason": "safety_blocked"}

    @pytest.mark.parametrize("state", ["open", "stop"])
    def test_saving_and_reading_the_plan_are_not_closed_by_it(self, goal, state) -> None:
        """«Не зависимые действия»: человек с ограничением план сохранит и
        увидит — но ни один шаг до услуги не доведёт."""
        from wellness.tests.test_plan_engine_steps_2868 import _command, _step

        saved = _api().post(PLAN_URL, {**_command(goal, [_step("s1")]), "s1_restriction": state}, format="json")
        assert saved.status_code == 201, saved.content
        assert _api().get(PLAN_URL).json()["data"]["plan"]["plan_id"] == saved.json()["data"]["plan"]["plan_id"]

    def test_once_the_bot_says_none_the_step_goes_on(self, goal, massage) -> None:
        """Каталог ограничение не хранит и не снимает — читает присланное."""
        _, offering = massage
        plan = _save(goal)
        assert _candidates(plan, s1_restriction="open").status_code == 409
        search = _found(plan)["search_id"]
        assert _choose(plan, offering, search).status_code == 201
