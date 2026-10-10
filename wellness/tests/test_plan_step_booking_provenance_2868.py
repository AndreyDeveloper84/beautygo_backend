# flake8: noqa: F811 — фикстуры импортированы из узлов шага и приходят параметрами
"""Запись от шага плана: происхождение в создании записи (DRF-2868, решение владельца 07.10).

Условия владельца, по узлу на каждое:

* обычная запись без плана работает без этих полей — как прежде;
* сервер проверяет: план принадлежит человеку, шаг относится к плану,
  выбранная услуга соответствует разрешённому переходу из шага;
* запись и её связь с шагом сохраняются ВМЕСТЕ: без частичного успеха и без
  дублей при повторе запроса;
* переданные идентификаторы не обходят проверки допуска (включая безопасность
  хода и гейт здоровья самой записи);
* бронь видна в шаге, но не означает достижения цели или завершения плана.

Записи создаются настоящей ручкой ``POST /internal/appointments/``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from appointments.models import Appointment, OutboxEvent
from goals.models import ClientGoal
from services.models import ServiceTemplate
from wellness.models import Plan, PlanStepBooking
from wellness.tests.test_plan_engine_steps_2868 import (  # noqa: F401 — фикстуры того же сценария
    APPOINTMENTS_URL,
    OWNER,
    PLAN_URL,
    SAFETY,
    STRANGER,
    _api,
    _offer,
    _resolve,
    _save,
    _step,
    _admission_is_not_the_subject,
    _confirmed_knowledge,
    _to_offer,
    _token_and_flag,
    canon,
    category,
    goal,
    offer,
    other_canon,
    other_offer,
    owner,
    specialist,
    stranger,
    tenant,
)

pytestmark = pytest.mark.django_db


def _start(hours: int = 3) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0).isoformat()


def _provenance(plan: Plan, step_id: str = "s1", **over) -> dict:
    return {"entry_point": "PLAN_STEP", "plan_id": str(plan.id), "step_id": step_id, **SAFETY, **over}


def _create(
    who, specialist, offer, *, provenance=None, key: str | None = None, hours: int = 3, as_user: str | None = None,
    payment_required: bool = False,
):
    body = {
        "client_id": str(who.id), "specialist_id": str(specialist.id), "service_id": str(offer.id),
        "start_datetime": _start(hours), "payment_required": payment_required,
    }
    if provenance is not None:
        body["provenance"] = provenance
    return _api(as_user or who.username).post(
        APPOINTMENTS_URL, body, format="json", HTTP_X_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


@pytest.fixture
def plan(goal, canon, offer) -> Plan:
    plan = _save(goal)
    _to_offer(plan, canon, offer)
    return plan


def _nothing_was_written() -> None:
    assert not Appointment.objects.exists()
    assert not PlanStepBooking.objects.exists()
    assert not OutboxEvent.objects.exists()


class TestOrdinaryBookingIsUntouched:
    def test_without_the_block_a_booking_is_created_and_no_plan_is_involved(self, plan, owner, specialist, offer) -> None:
        resp = _create(owner, specialist, offer)
        assert resp.status_code == 201, resp.content
        assert Appointment.objects.count() == 1
        assert not PlanStepBooking.objects.exists()

    def test_without_the_block_the_engine_flag_does_not_matter(self, owner, specialist, offer, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        assert _create(owner, specialist, offer).status_code == 201

    def test_the_outbox_payload_of_a_step_booking_has_the_same_shape(self, plan, owner, specialist, offer) -> None:
        """Событие о записи не начинает нести план: обычный потребитель события
        видит ту же форму."""
        assert _create(owner, specialist, offer).status_code == 201
        plain = {e.topic: set(e.payload["data"]) for e in OutboxEvent.objects.all()}
        OutboxEvent.objects.all().delete()
        assert _create(owner, specialist, offer, provenance=_provenance(plan), hours=6).status_code == 201
        stepped = {e.topic: set(e.payload["data"]) for e in OutboxEvent.objects.all()}
        assert plain and stepped == plain


class TestBookingAndLinkTogether:
    def test_the_booking_and_its_link_are_created_by_one_call(self, plan, owner, specialist, offer, goal) -> None:
        before = (Plan.objects.values().get(), ClientGoal.objects.values().get())
        resp = _create(owner, specialist, offer, provenance=_provenance(plan))
        assert resp.status_code == 201, resp.content
        appt = Appointment.objects.get()
        link = PlanStepBooking.objects.get()
        assert (link.appointment_id, link.plan_id, link.step_id) == (appt.id, plan.id, "s1")
        assert (link.safety_state, link.safety_evaluated_at_revision) == ("NORMAL", SAFETY["evaluated_at_revision"])
        # Бронь видна в шаге — и только: ни план, ни цель не изменились.
        doc = _api().get(PLAN_URL).json()["data"]["plan"]
        assert doc["step_state"]["s1"]["bookings"][0]["appointment_id"] == str(appt.id)
        assert doc["status"] == "active" and doc["goal_state"] == "active"
        assert (Plan.objects.values().get(), ClientGoal.objects.values().get()) == before

    def test_a_repeat_of_the_request_is_the_same_booking_and_one_link(self, plan, owner, specialist, offer) -> None:
        key = str(uuid.uuid4())
        first = _create(owner, specialist, offer, provenance=_provenance(plan), key=key)
        second = _create(owner, specialist, offer, provenance=_provenance(plan), key=key)
        assert first.status_code == 201 and second.status_code == 201, (first.content, second.content)
        assert first.json()["data"]["id"] == second.json()["data"]["id"]
        assert (Appointment.objects.count(), PlanStepBooking.objects.count()) == (1, 1)

    def test_a_repeat_is_not_judged_again_even_under_stop(self, plan, owner, specialist, offer) -> None:
        """Повтор уже созданной записи возвращает её: решение принято один раз."""
        key = str(uuid.uuid4())
        assert _create(owner, specialist, offer, provenance=_provenance(plan), key=key).status_code == 201
        again = _create(owner, specialist, offer, provenance=_provenance(plan, safety_state="STOP"), key=key)
        assert again.status_code == 201
        assert (Appointment.objects.count(), PlanStepBooking.objects.count()) == (1, 1)

    def test_a_failure_while_linking_leaves_no_booking(self, plan, owner, specialist, offer, monkeypatch) -> None:
        def _boom(*args, **kwargs):
            raise RuntimeError("link write failed")

        monkeypatch.setattr(PlanStepBooking.objects, "create", _boom)
        with pytest.raises(RuntimeError):
            _create(owner, specialist, offer, provenance=_provenance(plan))
        _nothing_was_written()


class TestIdentifiersDoNotBypassAdmission:
    def _refused(self, resp, status: int, reason: str) -> None:
        assert resp.status_code == status, resp.content
        assert resp.json()["error"]["details"]["reason"] == reason
        _nothing_was_written()

    def test_another_persons_plan(self, plan, stranger, specialist, offer) -> None:
        resp = _create(stranger, specialist, offer, provenance=_provenance(plan))
        self._refused(resp, 404, "plan_not_found")

    def test_a_step_that_is_not_in_the_plan(self, plan, owner, specialist, offer) -> None:
        self._refused(_create(owner, specialist, offer, provenance=_provenance(plan, "s404")), 404, "step_not_found")

    def test_a_service_the_step_was_not_resolved_to(self, plan, owner, specialist, other_offer) -> None:
        resp = _create(owner, specialist, other_offer, provenance=_provenance(plan))
        self._refused(resp, 409, "offer_mismatch")

    def test_a_step_still_at_capability_level(self, plan, owner, specialist, offer) -> None:
        self._refused(
            _create(owner, specialist, offer, provenance=_provenance(plan, "s2")), 409, "step_not_offer_level",
        )

    @pytest.mark.parametrize("state", ["STOP", "UNKNOWN"])
    def test_blocking_safety(self, plan, owner, specialist, offer, state) -> None:
        resp = _create(owner, specialist, offer, provenance=_provenance(plan, safety_state=state))
        self._refused(resp, 409, "safety_blocked")

    @pytest.mark.parametrize("state, reason", [("open", "s1_restriction_open"), ("stop", "s1_restriction_stop")])
    def test_the_durable_restriction_of_the_person(self, plan, owner, specialist, offer, state, reason) -> None:
        """Запись от шага — действие, ведущее к услуге: длительное ограничение
        S1 её закрывает, и записи не появляется."""
        resp = _create(owner, specialist, offer, provenance=_provenance(plan, s1_restriction=state))
        self._refused(resp, 409, reason)

    @pytest.mark.parametrize("status", ["paused", "archived", "superseded"])
    def test_a_plan_that_is_not_active(self, plan, owner, specialist, offer, status) -> None:
        Plan.objects.filter(pk=plan.pk).update(status=status)
        self._refused(_create(owner, specialist, offer, provenance=_provenance(plan)), 409, "plan_not_in_effect")

    def test_a_blocked_step(self, goal, canon, offer, owner, specialist) -> None:
        blocked = _save(
            goal,
            [_step("s1", level="OFFER", canonical_service_ref=str(canon.id), tenant_offer_ref=str(offer.id))],
            {"s1": "BLOCKED"},
        )
        self._refused(_create(owner, specialist, offer, provenance=_provenance(blocked)), 409, "step_blocked")

    def test_the_engine_flag_off(self, plan, owner, specialist, offer, settings) -> None:
        settings.PLAN_ENGINE_ENABLED = False
        resp = _create(owner, specialist, offer, provenance=_provenance(plan))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"
        _nothing_was_written()

    def test_under_test_data_a_person_outside_the_list_has_no_engine(self, plan, owner, specialist, offer, settings) -> None:
        """Вторая линия изоляции (владелец 10.10): пока включены тестовые данные,
        запись от шага человеку вне серверного списка отвечает как при
        выключенном движке — путь идёт через ручку записи, не через ручки Плана."""
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []
        resp = _create(owner, specialist, offer, provenance=_provenance(plan))
        assert resp.status_code == 404, resp.content
        assert resp.json()["error"]["code"] == "PLAN_ENGINE_DISABLED"
        _nothing_was_written()

    def test_under_test_data_an_ordinary_booking_without_the_block_is_untouched(
        self, owner, specialist, offer, settings,
    ) -> None:
        settings.SYNTHETIC_TEST_DATA_ENABLED = True
        settings.SYNTHETIC_TEST_SUBJECT_IDS = []
        assert _create(owner, specialist, offer).status_code == 201

    @pytest.mark.parametrize(
        ("patch", "reason"),
        [
            ({"entry_point": "RECOMMENDATION"}, "entry_point_unsupported"),
            ({"entry_point": None}, "entry_point_unsupported"),
            ({"plan_id": "not-a-uuid"}, "plan_id_malformed"),
            ({"step_id": ""}, "step_id_missing"),
            ({"safety_state": "NOT_APPLICABLE"}, "safety_state_invalid"),
            ({"safety_state": None}, "safety_state_invalid"),
            ({"safety_policy_version": ""}, "safety_policy_version_missing"),
            ({"evaluated_at_revision": None}, "safety_revision_malformed"),
            # Молчание об ограничении — не его отсутствие.
            ({"s1_restriction": None}, "s1_restriction_invalid"),
            ({"s1_restriction": "cleared"}, "s1_restriction_invalid"),
        ],
    )
    def test_a_malformed_block_is_refused_and_nothing_is_booked(self, plan, owner, specialist, offer, patch, reason) -> None:
        resp = _create(owner, specialist, offer, provenance=_provenance(plan, **patch))
        assert resp.status_code == 400, resp.content
        assert resp.json()["error"]["code"] == "PLAN_CONTRACT_VIOLATION"
        assert resp.json()["error"]["details"]["reason"] == reason
        _nothing_was_written()

    def test_the_block_does_not_lift_the_health_gate_of_the_booking(
        self, goal, canon, tenant, category, specialist, owner,
    ) -> None:
        """Шаг разрешён до услуги, чей канон требует расспроса о здоровье: блок
        происхождения гейт здоровья самой записи не снимает."""
        screened = _offer(tenant, category, canon, specialist, "Требует расспроса")
        plan = _save(goal)
        _to_offer(plan, canon, screened)
        # Флаг на каноне поднят уже после того, как шаг дошёл до услуги.
        ServiceTemplate.objects.filter(pk=canon.pk).update(requires_health_check=True)
        resp = _create(owner, specialist, screened, provenance=_provenance(plan))
        assert resp.status_code == 422, resp.content
        assert resp.json()["error"]["code"] == "HEALTH_CHECK_REQUIRED"
        _nothing_was_written()

    def test_the_control_the_same_booking_passes_without_the_raised_flag(
        self, goal, canon, tenant, category, specialist, owner,
    ) -> None:
        screened = _offer(tenant, category, canon, specialist, "Требует расспроса")
        plan = _save(goal)
        _to_offer(plan, canon, screened)
        assert _create(owner, specialist, screened, provenance=_provenance(plan)).status_code == 201

    def test_the_client_id_cross_check_still_stands(self, plan, owner, stranger, specialist, offer) -> None:
        resp = _create(owner, specialist, offer, provenance=_provenance(plan), as_user=STRANGER)
        assert resp.status_code == 403
        _nothing_was_written()


# ─── гейт согласия и удаления у записи от шага (решение владельца 09.10) ─────


class TestABookingFromAMarkedPlanIsNeverPrepaid:
    """DRF-2871: помеченные синтетические данные проверяют механику пути —
    реальных списаний по ним не бывает (владелец 08.10). Предохранитель на
    сервере и от вызывающего не зависит: запись от шага плана, помеченного
    синтетическим, создаётся без предоплаты, что бы ни прислали."""

    @pytest.fixture
    def marked(self, monkeypatch):
        """План шага читается как помеченный. Пометка в базе несмываема и
        ставится только сохранением на синтетическом знании — этот путь
        держат ``test_plan_synthetic_data_2871`` и узлы кандидатов; здесь
        предмет — правило предоплаты, поэтому пометка подставлена на
        прочитанном плане, а остальной путь записи настоящий."""
        from wellness import plan_engine_steps

        real = plan_engine_steps.admit_step_for_booking

        def admitted(*args, **kwargs):
            plan = real(*args, **kwargs)
            plan.synthetic = True
            return plan

        monkeypatch.setattr(plan_engine_steps, "admit_step_for_booking", admitted)

    def _log(self, caplog):
        from appointments.tests.test_payment_required_server_decides_b61 import _capturing_the_service_log

        return _capturing_the_service_log(caplog)

    def test_a_prepayment_asked_for_a_marked_plan_is_refused_by_the_server(
        self, plan, owner, specialist, offer, marked, caplog,
    ) -> None:
        with self._log(caplog):
            resp = _create(owner, specialist, offer, provenance=_provenance(plan), payment_required=True)
        assert resp.status_code == 201, resp.content
        booking = Appointment.objects.get()
        assert booking.payments.count() == 0
        assert booking.status == Appointment.Status.CONFIRMED
        assert PlanStepBooking.objects.filter(appointment=booking).count() == 1
        [refusal] = [r for r in caplog.records if "booking.payment_required_refused" in r.getMessage()]
        assert "reason=synthetic_test_data" in refusal.getMessage()
        assert "requested=True applied=False" in refusal.getMessage()

    def test_no_prepayment_asked_is_not_counted_as_a_refusal(self, plan, owner, specialist, offer, marked, caplog) -> None:
        with self._log(caplog):
            resp = _create(owner, specialist, offer, provenance=_provenance(plan), payment_required=False)
        assert resp.status_code == 201, resp.content
        assert Appointment.objects.get().payments.count() == 0
        assert [r for r in caplog.records if "payment_required_refused" in r.getMessage()] == []

    def test_the_control_an_ordinary_plan_keeps_the_prepayment_it_asked_for(
        self, plan, owner, specialist, offer, caplog,
    ) -> None:
        """Без пометки правило не срабатывает: запись клиента от шага ждёт оплату, как раньше."""
        with self._log(caplog):
            resp = _create(owner, specialist, offer, provenance=_provenance(plan), payment_required=True)
        assert resp.status_code == 201, resp.content
        booking = Appointment.objects.get()
        assert booking.payments.count() == 1
        assert booking.status == Appointment.Status.AWAITING_PAYMENT
        assert [r for r in caplog.records if "payment_required_refused" in r.getMessage()] == []


class TestTheStepBookingIsUnderThePlanGate:
    """«Нельзя переходить к записи или создавать запись» — у записи ОТ ШАГА ПЛАНА.

    Обычная запись без блока шага сюда не относится: что делать с ней при
    заявке на удаление, спецификация гейта плана не решает.
    """

    @staticmethod
    def _request_deletion(user):
        from datetime import timedelta

        from django.utils import timezone

        from users.models import DeletionRequest

        return DeletionRequest.objects.create(
            user=user, initiator="bot", status=DeletionRequest.Status.REQUESTED,
            deadline_at=timezone.now() + timedelta(days=30),
        )

    def test_a_deletion_request_closes_the_booking_from_a_step(self, plan, owner, specialist, offer) -> None:
        request = self._request_deletion(owner)

        response = _create(owner, specialist, offer, provenance=_provenance(plan))

        assert response.status_code == 423, response.content[:300]
        assert response.json()["error"]["details"]["request_id"] == str(request.pk)
        _nothing_was_written()

    def test_a_booking_from_a_step_without_an_attestation_is_refused(self, plan, owner, specialist, offer) -> None:
        """«Согласия нет — блок»: запись от шага плана без утверждения основания не создаётся."""
        body = {
            "client_id": str(owner.id), "specialist_id": str(specialist.id), "service_id": str(offer.id),
            "start_datetime": _start(3), "payment_required": False, "provenance": _provenance(plan),
        }

        response = _api(owner.username).post(
            APPOINTMENTS_URL, body, format="json", attested=False, HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        assert response.status_code == 422, response.content[:300]
        assert response.json()["error"]["details"]["reason"] == "not_attested"
        _nothing_was_written()

    def test_a_withdrawn_storage_consent_closes_the_booking_from_a_step(self, plan, owner, specialist, offer) -> None:
        from django.utils import timezone

        from users.consent_events import PERSONAL_DATA
        from users.models import ConsentState

        ConsentState.objects.create(
            user=owner, consent_type=PERSONAL_DATA, granted=False, granted_at=timezone.now(), event_id="gate-booking",
        )

        response = _create(owner, specialist, offer, provenance=_provenance(plan))

        assert response.status_code == 422, response.content[:300]
        assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
        _nothing_was_written()

    def test_an_ordinary_booking_is_not_judged_by_the_plan_gate(self, plan, owner, specialist, offer) -> None:
        """Контроль области: без блока шага гейт плана не вызывается — ни заявка, ни отсутствие утверждения."""
        self._request_deletion(owner)
        body = {
            "client_id": str(owner.id), "specialist_id": str(specialist.id), "service_id": str(offer.id),
            "start_datetime": _start(3), "payment_required": False,
        }

        response = _api(owner.username).post(
            APPOINTMENTS_URL, body, format="json", attested=False, HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

        assert "consent" not in body
        assert response.status_code == 201, response.content[:300]
