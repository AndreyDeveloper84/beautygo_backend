"""Шаг плана ↔ услуга ↔ запись (DRF-2868, Plan WP2).

Контракт PLAN_ENGINE_CONTRACT v1.0 §4.3, §8.2, §8.3; уточнение 3 пакета
прохода 2. Что держат узлы:

* переход шага — только вниз, только по решению резолвера, только на канон и
  предложение, которые существуют и связаны между собой; строка дописывается
  и не правится;
* запись на шаге — ФАКТ: связь не меняет ни план, ни цель; отмена записи
  видна в чтении сама и тоже ничего не меняет;
* допуск шага проверяет сервер, по строке на каждое условие;
* ``recommendation_id`` пуст и ничем не подменяется (решение 07.10, В-2(а));
* чужой план, чужая запись — «не найдено»;
* стирание: связи уходят с планом, запись остаётся.

Записи создаются настоящей ручкой ``POST /internal/appointments/`` — тем же
путём, что у бота, а не строкой в таблицу.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from appointments.models import Appointment
from goals.models import ClientGoal
from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User
from wellness.models import (
    PLAN_FORBIDDEN_FIELDS,
    ImmutablePlanRecordError,
    Plan,
    PlanRevision,
    PlanStepBooking,
    PlanStepResolution,
)
from wellness.plan_engine import ContractViolation, create_plan_from_command, parse_command

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-steps"
PLAN_URL = "/api/v1/internal/me/plan/"
RESOLUTION_URL = "/api/v1/internal/me/plan/steps/resolution/"
BOOKING_URL = "/api/v1/internal/me/plan/steps/booking/"
APPOINTMENTS_URL = "/api/v1/internal/appointments/"
OWNER = "bot:plan-steps-owner"
STRANGER = "bot:plan-steps-stranger"

POLICY_VERSIONS = {
    "plan_spec_version": "1.0",
    "constraint_policy_version": "1.0",
    "resolver_spec_version": "1.0",
    "catalog_mapping_version": "2026-10-07",
    "safety_policy_version": "0.12",
    "reason_code_registry_version": "3",
}
DECISION = "resolver-decision-1"
#: DRF-2877 — каждое действие с шагом несёт безопасность хода.
SAFETY = {
    "safety_state": "NORMAL", "safety_policy_version": "pre_check-test", "evaluated_at_revision": 4,
    # DRF-2868 — длительное ограничение S1: действия, ведущие к услуге и записи, обязаны его назвать.
    "s1_restriction": "none",
}


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_ENGINE_ENABLED = True


@pytest.fixture(autouse=True)
def _admission_is_not_the_subject(monkeypatch):
    """Предмет этих узлов — правила шага: уровни, запись как факт, гейты.
    Допуск предложения как кандидата шага (поиск по способности, восемь
    проверок, происхождение ответа о здоровье) держит
    ``test_plan_step_candidates_2868.py`` — на настоящих данных, без подмен.
    Здесь он снят, чтобы узлы уровня не зависели от разметки услуги."""
    monkeypatch.setattr("wellness.plan_engine_steps.offer_is_candidate", lambda *a, **k: True)


def _user(username: str, phone: str) -> User:
    return User.objects.create_user(
        username=username, password="x", role="client", phone=phone, is_proxy=True,
    )


@pytest.fixture
def owner(db) -> User:
    return _user(OWNER, "+79995028681")


@pytest.fixture
def stranger(db) -> User:
    return _user(STRANGER, "+79995028682")


@pytest.fixture
def goal(owner) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key="relax", source_channel="bot")


@pytest.fixture
def tenant(db) -> Tenant:
    return Tenant.objects.create(slug="plan-steps-t", name="Plan Steps Tenant")


@pytest.fixture
def specialist(tenant) -> SpecialistProfile:
    u = User.objects.create_user(
        username="plan_steps_spec", password="x", role="specialist", phone="+79995028683",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Plan Steps Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db) -> ServiceCategory:
    return ServiceCategory.objects.create(name="Plan Steps Cat", slug="plan-steps-cat")


@pytest.fixture
def canon(category) -> ServiceTemplate:
    return ServiceTemplate.objects.create(category=category, name="Массаж спины")


@pytest.fixture
def other_canon(category) -> ServiceTemplate:
    return ServiceTemplate.objects.create(category=category, name="Массаж всего тела")


def _offer(tenant, category, template, specialist, name: str) -> SalonService:
    salon = SalonService.objects.create(
        tenant=tenant, category=category, template=template, name=name,
        duration_minutes=60, requires_health_check=False,
    )
    SpecialistService.objects.create(
        salon_service=salon, specialist=specialist, duration_minutes=None,
        price=Decimal("2000.00"), buffer_after_minutes=15, is_active=True,
    )
    return salon


@pytest.fixture
def offer(tenant, category, canon, specialist) -> SalonService:
    return _offer(tenant, category, canon, specialist, "Массаж спины 60")


@pytest.fixture
def other_offer(tenant, category, other_canon, specialist) -> SalonService:
    return _offer(tenant, category, other_canon, specialist, "Массаж тела 60")


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _step(step_id: str, **over) -> dict:
    step = {
        "step_id": step_id, "role": "CORE", "outcome_ref": "relax",
        "capability_ref": "cap:relaxation-massage", "level": "CAPABILITY",
        "canonical_service_ref": None, "tenant_offer_ref": None, "assertions": [],
        "execution": None, "alternative_of": None, "provenance": {"engine_version": "0.1"},
    }
    step.update(over)
    return step


def _command(goal: ClientGoal, steps: list[dict], verdicts: dict[str, str] | None = None) -> dict:
    verdicts = verdicts or {s["step_id"]: "INCOMPLETE" for s in steps}
    return {
        "decision_id": str(uuid.uuid4()),
        "goal_ref": str(goal.id),
        "confirmation": {"question_id": "plan.save", "option_id": "yes", "state_revision": 1},
        **SAFETY,
        "provenance": {"policy_versions": dict(POLICY_VERSIONS)},
        "decision": {
            "steps": steps, "assertions": [],
            "validation": {"status": "INCOMPLETE", "step_validations": verdicts},
        },
    }


def _save(goal: ClientGoal, steps: list[dict] | None = None, verdicts=None) -> Plan:
    steps = steps if steps is not None else [_step("s1"), _step("s2", role="OPTIONAL")]
    plan, created = create_plan_from_command(goal.client, parse_command(_command(goal, steps, verdicts)))
    assert created
    return plan


def _resolve(plan: Plan, step_id: str = "s1", who: str = OWNER, **body):
    return _api(who).post(
        RESOLUTION_URL,
        {"plan_id": str(plan.id), "step_id": step_id, "resolver_decision_id": DECISION, **SAFETY, **body},
        format="json",
    )


def _to_offer(plan: Plan, canon: ServiceTemplate, offer: SalonService, step_id: str = "s1"):
    resp = _resolve(
        plan, step_id, level="OFFER",
        canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
    )
    assert resp.status_code == 201, resp.content
    return resp


def _book(who: User, specialist: SpecialistProfile, offer: SalonService, *, hours: int = 3) -> Appointment:
    start = (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)
    resp = _api(who.username).post(
        APPOINTMENTS_URL,
        {
            "client_id": str(who.id), "specialist_id": str(specialist.id),
            "service_id": str(offer.id), "start_datetime": start.isoformat(),
            "payment_required": False,
        },
        format="json",
        HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert resp.status_code in (200, 201), resp.content
    appt = Appointment.objects.filter(client=who).order_by("-created_at").first()
    assert appt is not None and appt.salon_service_id == offer.id
    return appt


def _link(plan: Plan, appt: Appointment, step_id: str = "s1", who: str = OWNER, **body):
    return _api(who).post(
        BOOKING_URL,
        {"plan_id": str(plan.id), "step_id": step_id, "appointment_id": str(appt.id), **SAFETY, **body},
        format="json",
    )


# ─── 0. валидация по каждому шагу обязательна ────────────────────────────────


class TestStepValidationsShape:
    @pytest.mark.parametrize(
        "verdicts",
        [None, {}, {"s1": "INCOMPLETE"}, {"s1": "VALID", "s2": "VALID", "s3": "VALID"},
         {"s1": "VALID", "s2": "OK"}, ["s1", "s2"]],
    )
    def test_a_command_without_a_verdict_for_every_step_is_refused(self, goal, verdicts) -> None:
        body = _command(goal, [_step("s1"), _step("s2")])
        if verdicts is None:
            del body["decision"]["validation"]["step_validations"]
        else:
            body["decision"]["validation"]["step_validations"] = verdicts
        with pytest.raises(ContractViolation) as exc:
            parse_command(body)
        assert exc.value.reason == "step_validations_malformed"


# ─── 1. переход шага вниз (§4.3, §8.2) ───────────────────────────────────────


class TestResolveStep:
    def test_capability_to_offer_is_recorded_outside_the_revision(self, goal, canon, offer) -> None:
        plan = _save(goal)
        snapshot_before = PlanRevision.objects.get().steps_snapshot
        resp = _to_offer(plan, canon, offer)
        row = PlanStepResolution.objects.get()
        assert (row.level, row.canonical_service_id, row.tenant_offer_id) == ("OFFER", canon.id, offer.id)
        assert row.resolver_decision_id == DECISION
        # Снимок ревизии не тронут: шаг в нём по-прежнему CAPABILITY.
        assert PlanRevision.objects.get().steps_snapshot == snapshot_before
        assert snapshot_before[0]["level"] == "CAPABILITY"
        state = resp.json()["data"]["plan"]["step_state"]["s1"]
        assert state["level"] == "OFFER"
        assert state["tenant_offer_ref"] == str(offer.id)
        assert state["resolver_decision_id"] == DECISION
        # Соседний шаг остался, каким был.
        assert resp.json()["data"]["plan"]["step_state"]["s2"]["level"] == "CAPABILITY"

    def test_service_then_offer(self, goal, canon, offer) -> None:
        plan = _save(goal)
        assert _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id)).status_code == 201
        _to_offer(plan, canon, offer)
        assert sorted(PlanStepResolution.objects.values_list("level", flat=True)) == ["OFFER", "SERVICE"]

    def test_the_recommendation_id_is_empty_and_not_invented(self, goal, canon, offer) -> None:
        """В-2(а): живой Recommendation нет — поле пусто, decision_id в него не кладут."""
        plan = _save(goal)
        resp = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
            recommendation_id=str(uuid.uuid4()),
        )
        assert resp.status_code == 201
        assert PlanStepResolution.objects.get().recommendation_id is None
        assert resp.json()["data"]["plan"]["step_state"]["s1"]["recommendation_id"] is None

    def test_a_repeat_is_the_same_row(self, goal, canon, offer) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        again = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
        )
        assert again.status_code == 200 and again.json()["data"]["created"] is False
        assert PlanStepResolution.objects.count() == 1

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            ({"level": "CAPABILITY"}, "level_invalid"),
            ({"level": "OFFER"}, "level_refs_mismatch"),
            ({"level": "SERVICE", "tenant_offer_ref": "OFFER"}, "level_refs_mismatch"),
            ({"level": "SERVICE", "resolver_decision_id": ""}, "resolver_decision_missing"),
            ({"level": "SERVICE", "resolver_decision_id": None}, "resolver_decision_missing"),
            ({"level": "SERVICE", "canonical_service_ref": "UNKNOWN"}, "canonical_service_unknown"),
            ({"level": "OFFER", "tenant_offer_ref": "UNKNOWN"}, "tenant_offer_unknown"),
            ({"level": "OFFER", "tenant_offer_ref": "OTHER"}, "offer_not_of_canonical_service"),
        ],
    )
    def test_each_refusal_by_name(self, goal, canon, offer, other_offer, body, reason) -> None:
        plan = _save(goal)
        refs = {"OFFER": str(offer.id), "OTHER": str(other_offer.id), "UNKNOWN": str(uuid.uuid4())}
        payload = {"canonical_service_ref": str(canon.id), **body}
        for key in ("tenant_offer_ref", "canonical_service_ref"):
            if payload.get(key) in refs:
                payload[key] = refs[payload[key]]
        resp = _resolve(plan, **payload)
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_RESOLUTION_REFUSED"
        assert resp.json()["error"]["details"]["reason"] == reason
        assert not PlanStepResolution.objects.exists()

    def test_an_offer_outside_the_canon_is_unknown(self, goal, canon, tenant, category, specialist) -> None:
        loose = _offer(tenant, category, None, specialist, "Вне канона")
        plan = _save(goal)
        resp = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(loose.id),
        )
        assert resp.json()["error"]["details"]["reason"] == "tenant_offer_unknown"

    def test_no_way_back_up_and_no_second_answer_for_a_level(self, goal, canon, offer, other_canon) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        up = _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id))
        assert up.json()["error"]["details"]["reason"] == "not_downward"
        other = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
            resolver_decision_id="another-decision",
        )
        assert other.json()["error"]["details"]["reason"] == "level_already_resolved"
        assert PlanStepResolution.objects.count() == 1

    def test_the_canon_chosen_at_service_level_cannot_be_swapped(
        self, goal, canon, other_canon, other_offer,
    ) -> None:
        plan = _save(goal)
        assert _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id)).status_code == 201
        resp = _resolve(
            plan, level="OFFER", canonical_service_ref=str(other_canon.id),
            tenant_offer_ref=str(other_offer.id),
        )
        assert resp.json()["error"]["details"]["reason"] == "canonical_service_changed"

    def test_a_step_already_at_offer_in_the_snapshot_has_nowhere_to_go(self, goal, canon, offer) -> None:
        plan = _save(goal, [_step(
            "s1", level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
        )])
        resp = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id),
        )
        assert resp.json()["error"]["details"]["reason"] == "not_downward"

    def test_the_row_is_append_only(self, goal, canon, offer) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        row = PlanStepResolution.objects.get()
        row.resolver_decision_id = "rewritten"
        with pytest.raises(ImmutablePlanRecordError):
            row.save()
        with pytest.raises(ImmutablePlanRecordError):
            PlanStepResolution.objects.filter(pk=row.pk).update(resolver_decision_id="rewritten")
        with pytest.raises(ImmutablePlanRecordError):
            row.delete()
        with pytest.raises(ImmutablePlanRecordError):
            PlanStepResolution.objects.filter(pk=row.pk).delete()
        assert PlanStepResolution.objects.get().resolver_decision_id == DECISION

    def test_the_database_refuses_an_offer_level_without_an_offer(self, goal, canon) -> None:
        plan = _save(goal)
        with pytest.raises(IntegrityError), transaction.atomic():
            PlanStepResolution.objects.create(
                plan_revision=plan.current_revision, step_id="s1", level="OFFER",
                canonical_service=canon, resolver_decision_id=DECISION,
                safety_state="NORMAL", safety_policy_version="v", safety_evaluated_at_revision=0,
            )

    def test_unknown_step_and_strangers_plan_are_not_found(self, goal, canon, stranger) -> None:
        plan = _save(goal)
        missing = _resolve(plan, "s404", level="SERVICE", canonical_service_ref=str(canon.id))
        assert missing.status_code == 404
        assert missing.json()["error"]["details"]["reason"] == "step_not_found"
        theirs = _resolve(plan, who=STRANGER, level="SERVICE", canonical_service_ref=str(canon.id))
        assert theirs.status_code == 404
        assert theirs.json()["error"]["details"]["reason"] == "plan_not_found"
        assert not PlanStepResolution.objects.exists()


# ─── 2. допуск шага (уточнение 3 пакета) ─────────────────────────────────────


class TestStepGate:
    """Одна строка таблицы допуска — один узел; и переход, и запись отказывают."""

    @pytest.fixture
    def ready(self, goal, canon, offer, specialist, owner):
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        return plan, _book(owner, specialist, offer)

    def _refused(self, resp, reason: str) -> None:
        assert resp.status_code == 409, resp.content
        assert resp.json()["error"]["code"] == "PLAN_STEP_NOT_EXECUTABLE"
        assert resp.json()["error"]["details"]["reason"] == reason
        assert not PlanStepBooking.objects.exists()

    @pytest.mark.parametrize("status", ["paused", "archived", "superseded"])
    def test_a_plan_that_is_not_active(self, ready, status) -> None:
        plan, appt = ready
        Plan.objects.filter(pk=plan.pk).update(status=status)
        self._refused(_link(plan, appt), "plan_not_in_effect")

    @pytest.mark.parametrize("state", ["paused", "achieved", "archived"])
    def test_a_goal_that_is_not_active(self, ready, goal, state) -> None:
        plan, appt = ready
        ClientGoal.objects.filter(pk=goal.pk).update(state=state)
        self._refused(_link(plan, appt), "plan_not_in_effect")

    def test_a_stale_revision(self, ready) -> None:
        plan, appt = ready
        # Мимо закрытой двери менеджера: узел ставит состояние, которое в бою
        # запишет пересчёт правил.
        from django.db import models

        models.QuerySet.update(
            PlanRevision._base_manager.filter(pk=plan.current_revision_id), staleness="stale_rules",
        )
        self._refused(_link(plan, appt), "revision_stale")

    def test_a_blocked_step(self, goal, canon, offer, specialist, owner) -> None:
        plan = _save(
            goal,
            [_step("s1", level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id))],
            {"s1": "BLOCKED"},
        )
        self._refused(_link(plan, _book(owner, specialist, offer)), "step_blocked")
        resolve = _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id))
        assert resolve.json()["error"]["details"]["reason"] == "step_blocked"

    def test_a_step_without_a_stored_verdict_is_not_admitted_by_default(self, ready) -> None:
        plan, appt = ready
        from django.db import models

        models.QuerySet.update(
            PlanRevision._base_manager.filter(pk=plan.current_revision_id),
            validation={"status": "INCOMPLETE"},
        )
        self._refused(_link(plan, appt), "step_validation_unknown")

    @pytest.mark.parametrize("level", ["CAPABILITY", "SERVICE"])
    def test_a_step_above_offer_level(self, goal, canon, offer, specialist, owner, level) -> None:
        plan = _save(goal)
        if level == "SERVICE":
            assert _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id)).status_code == 201
        self._refused(_link(plan, _book(owner, specialist, offer)), "step_not_offer_level")

    def test_a_booking_for_another_offer(self, ready, other_offer, specialist, owner) -> None:
        plan, _ = ready
        self._refused(_link(plan, _book(owner, specialist, other_offer, hours=6)), "offer_mismatch")

    def test_a_booking_made_before_the_plan(self, goal, canon, offer, specialist, owner) -> None:
        appt = _book(owner, specialist, offer)
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        self._refused(_link(plan, appt), "booking_predates_plan")

    def test_an_incomplete_step_at_offer_level_is_admitted(self, ready) -> None:
        """INCOMPLETE по необязательным сведениям записи не мешает (§6.3)."""
        plan, appt = ready
        assert plan.current_revision.validation["step_validations"]["s1"] == "INCOMPLETE"
        assert _link(plan, appt).status_code == 201


# ─── 3. запись — факт на шаге (§8.3) ─────────────────────────────────────────


class TestBookingIsAFact:
    @pytest.fixture
    def linked(self, goal, canon, offer, specialist, owner):
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        appt = _book(owner, specialist, offer)
        resp = _link(plan, appt)
        assert resp.status_code == 201, resp.content
        return plan, appt, resp

    def test_the_booking_shows_on_its_step_and_only_there(self, linked) -> None:
        plan, appt, resp = linked
        state = resp.json()["data"]["plan"]["step_state"]
        assert state["s1"]["bookings"] == [{
            "appointment_id": str(appt.id),
            "status": appt.status,
            "start_datetime": appt.start_datetime.isoformat(),
            "timezone": appt.snapshot_timezone,
        }]
        assert state["s2"]["bookings"] == []
        link = PlanStepBooking.objects.get()
        assert link.resolver_decision_id == DECISION and link.recommendation_id is None

    def test_the_booking_carries_the_zone_it_was_made_in(self, goal, canon, offer, specialist, owner) -> None:
        """Момент отдаётся в UTC; часы для человека — в поясе записи, не в
        запасном поясе вызывающего. Пояс не московский — чтобы узел не
        совпал с умолчанием."""
        type(specialist).objects.filter(pk=specialist.pk).update(timezone="Asia/Yekaterinburg")
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        appt = _book(owner, specialist, offer)
        resp = _link(plan, appt)
        assert resp.status_code == 201, resp.content
        [booking] = resp.json()["data"]["plan"]["step_state"]["s1"]["bookings"]
        assert booking["timezone"] == "Asia/Yekaterinburg"
        assert booking["start_datetime"].endswith("+00:00")

    def test_linking_changes_neither_the_plan_nor_the_goal(self, goal, canon, offer, specialist, owner) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        appt = _book(owner, specialist, offer)
        before = (Plan.objects.values().get(), ClientGoal.objects.values().get(), PlanRevision.objects.values().get())
        assert _link(plan, appt).status_code == 201
        after = (Plan.objects.values().get(), ClientGoal.objects.values().get(), PlanRevision.objects.values().get())
        assert after == before

    def test_a_cancelled_booking_is_seen_cancelled_and_changes_nothing(self, linked, goal) -> None:
        plan, appt, _ = linked
        before = (Plan.objects.values().get(), ClientGoal.objects.values().get())
        resp = _api().post(
            f"{APPOINTMENTS_URL}{appt.id}/cancel/", {"reason": "передумала"}, format="json",
            HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert resp.status_code == 200, resp.content
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CANCELLED
        # Факт остался, статус читается с записи; ни план, ни цель не изменились.
        doc = _api().get(PLAN_URL).json()["data"]["plan"]
        assert doc["step_state"]["s1"]["bookings"][0]["status"] == "cancelled"
        assert PlanStepBooking.objects.count() == 1
        assert (Plan.objects.values().get(), ClientGoal.objects.values().get()) == before

    def test_a_repeat_is_the_same_link(self, linked) -> None:
        plan, appt, _ = linked
        again = _link(plan, appt)
        assert again.status_code == 200 and again.json()["data"]["created"] is False
        assert PlanStepBooking.objects.count() == 1

    def test_one_booking_one_step(self, goal, canon, offer, specialist, owner) -> None:
        both = dict(level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id))
        plan = _save(goal, [_step("s1", **both), _step("s2", **both)])
        appt = _book(owner, specialist, offer)
        assert _link(plan, appt, "s1").status_code == 201
        resp = _link(plan, appt, "s2")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PLAN_STEP_BOOKING_CONFLICT"
        assert PlanStepBooking.objects.get().step_id == "s1"

    def test_two_bookings_on_one_step_are_two_facts_not_a_count(self, linked, specialist, offer, owner) -> None:
        plan, _, _ = linked
        second = _book(owner, specialist, offer, hours=8)
        resp = _link(plan, second)
        assert resp.status_code == 201
        state = resp.json()["data"]["plan"]["step_state"]["s1"]
        assert len(state["bookings"]) == 2
        assert set(state) == {
            "level", "canonical_service_ref", "tenant_offer_ref",
            "resolver_decision_id", "recommendation_id", "bookings",
            # DRF-2877 — на шаге или на плане незакрытый вопрос. Не признак
            # «выполнено» и не счётчик (PE-4): говорит, что шаг ждёт ответа.
            "restricted",
        }

    def test_the_document_carries_no_forbidden_key(self, linked) -> None:
        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        found = set(keys(_api().get(PLAN_URL).json()["data"]["plan"]))
        assert "bookings" in found  # присутствие раньше отсутствия
        assert not found & PLAN_FORBIDDEN_FIELDS
        for model in (PlanStepBooking, PlanStepResolution):
            assert not {f.name for f in model._meta.get_fields()} & PLAN_FORBIDDEN_FIELDS

    def test_a_strangers_booking_is_not_found(self, goal, canon, offer, specialist, stranger) -> None:
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        theirs = _book(stranger, specialist, offer)
        resp = _link(plan, theirs)
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "appointment_not_found"
        assert not PlanStepBooking.objects.exists()

    def test_a_strangers_plan_is_not_found(self, linked, stranger, specialist, offer) -> None:
        plan, _, _ = linked
        theirs = _book(stranger, specialist, offer, hours=9)
        resp = _link(plan, theirs, who=STRANGER)
        assert resp.status_code == 404
        assert resp.json()["error"]["details"]["reason"] == "plan_not_found"
        assert PlanStepBooking.objects.count() == 1

    def test_the_link_row_is_append_only(self, linked) -> None:
        link = PlanStepBooking.objects.get()
        link.step_id = "s2"
        with pytest.raises(ImmutablePlanRecordError):
            link.save()
        with pytest.raises(ImmutablePlanRecordError):
            PlanStepBooking.objects.filter(pk=link.pk).update(step_id="s2")
        with pytest.raises(ImmutablePlanRecordError):
            link.delete()
        with pytest.raises(ImmutablePlanRecordError):
            PlanStepBooking.objects.filter(pk=link.pk).delete()
        assert PlanStepBooking.objects.get().step_id == "s1"


# ─── 4. флаг ─────────────────────────────────────────────────────────────────


class TestFlag:
    def test_engine_off_both_step_writers_404(self, goal, canon, offer, specialist, owner, settings) -> None:
        plan = _save(goal)
        appt = _book(owner, specialist, offer)
        settings.PLAN_ENGINE_ENABLED = False
        for resp in (
            _resolve(plan, level="SERVICE", canonical_service_ref=str(canon.id)),
            _link(plan, appt),
        ):
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"
        assert not PlanStepResolution.objects.exists() and not PlanStepBooking.objects.exists()


# ─── 5. стирание и выгрузка ──────────────────────────────────────────────────


class TestErasureAndExport:
    @pytest.fixture
    def linked(self, goal, canon, offer, specialist, owner):
        plan = _save(goal)
        _to_offer(plan, canon, offer)
        appt = _book(owner, specialist, offer)
        assert _link(plan, appt).status_code == 201
        return plan, appt

    def test_forget_all_takes_the_links_with_the_plan_and_keeps_the_booking(self, linked, owner) -> None:
        from users.forget_all_catalog import erase_remembered_catalog

        _, appt = linked
        erase_remembered_catalog(owner, initiator="test")
        assert not Plan.objects.exists()
        assert not PlanStepResolution.objects.exists() and not PlanStepBooking.objects.exists()
        # «Бронирования — по закону» остаются.
        assert Appointment.objects.filter(pk=appt.pk).exists()

    def test_account_deletion_takes_the_links_and_keeps_the_anonymised_booking(self, linked, owner) -> None:
        from users.deletion_executor import BotConfirmation, execute
        from users.deletion_requests import ensure_deletion_request

        class _BotOk:
            def confirm(self, **kw):
                return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})

        _, appt = linked
        req = ensure_deletion_request(owner, initiator="bot").request
        out = execute(req, bot_client=_BotOk())
        assert out.completed, getattr(req, "failure_reason", "")
        assert not Plan.objects.exists()
        assert not PlanStepResolution.objects.exists() and not PlanStepBooking.objects.exists()
        appt.refresh_from_db()
        assert appt.client_id != owner.id

    def test_the_export_names_the_service_and_the_booking_time(self, linked, owner, canon) -> None:
        from users.remembered_export import export_wellness_plan

        _, appt = linked
        saved = export_wellness_plan(owner)["saved_plans"][0]
        assert saved["step_resolutions"] == [{
            "step_id": "s1", "level": "OFFER", "safety_state": "NORMAL",
            "created_at": saved["step_resolutions"][0]["created_at"],
            "canonical_service_name": canon.name, "has_tenant_offer": True,
        }]
        assert saved["step_bookings"][0]["step_id"] == "s1"
        assert saved["step_bookings"][0]["appointment_start"] == appt.start_datetime.isoformat()
        assert DECISION not in repr(saved)


# ─── слои раздельны: нет услуги под шаг ≠ нельзя сохранить план ──────────────
#
# Решение владельца 08.10: непроверенная (или отсутствующая) услуга не идёт в
# рекомендацию, но её отсутствие само по себе не запрещает сохранить допустимую
# общую часть плана. Что мешает подобрать услугу и что мешает сохранить — разные
# слои, и первый не протекает во второй.


class TestNoServiceForAStepIsNotNoPlan:
    def test_a_plan_is_saved_and_read_with_no_service_in_the_catalog_at_all(self, goal) -> None:
        assert not SalonService.objects.exists()
        saved = _api().post(PLAN_URL, _command(goal, [_step("s1"), _step("s2", role="OPTIONAL")]), format="json")
        assert saved.status_code == 201, saved.content
        plan = _api().get(PLAN_URL).json()["data"]["plan"]
        steps = plan["revision"]["steps"]
        assert [(s["step_id"], s["level"], s["tenant_offer_ref"]) for s in steps] == [
            ("s1", "CAPABILITY", None), ("s2", "CAPABILITY", None),
        ]
        assert plan["status"] == "active"

    def test_a_refused_way_to_a_service_leaves_the_saved_plan_whole(
        self, goal, canon, tenant, category, specialist, owner, offer,
    ) -> None:
        plan = _save(goal)
        before = _api().get(PLAN_URL).json()["data"]["plan"]

        refused = _resolve(
            plan, level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(uuid.uuid4()),
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "PLAN_STEP_RESOLUTION_REFUSED"
        # И запись от шага, не дошедшего до услуги, отказана своим слоем.
        link = _link(plan, _book(owner, specialist, offer))
        assert link.json()["error"]["details"]["reason"] == "step_not_offer_level"

        assert _api().get(PLAN_URL).json()["data"]["plan"] == before
        plan.refresh_from_db()
        assert plan.status == Plan.Status.ACTIVE
        assert not PlanStepResolution.objects.exists() and not PlanStepBooking.objects.exists()

    def test_the_other_step_still_goes_to_a_service(self, goal, canon, offer) -> None:
        """Отказ одному шагу — не ограничение соседнему и не ограничение плану."""
        plan = _save(goal)
        _resolve(plan, "s1", level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(uuid.uuid4()))
        assert _to_offer(plan, canon, offer, step_id="s2").status_code in (200, 201)
