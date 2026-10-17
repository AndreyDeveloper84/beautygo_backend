"""Запись на синтетическую услугу — только изолированным тестовым путём.

Решение владельца 10.10.2026: синтетическая запись через обычный клиентский
путь невозможна; санкционированная сквозная проверка идёт от шага плана,
помеченного синтетическим, и доступ сохраняет.

Набор засевается настоящей командой засева. Что держат узлы:

* обычная запись (без блока шага) на синтетическую услугу отказывает любому
  вызывающему — обычному человеку, тестовой персоне, даже под включённым
  стендом; идентификатор услуги пути не открывает;
* отказ тот же, что у услуги, которой нет, и ничего не пишет;
* запись от шага НАСТОЯЩЕГО плана на синтетическую услугу тоже невозможна;
* положительная пара: тот же человек той же услугой от шага помеченного
  плана записывается — сквозной путь «план → кандидат → выбор → запись».
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from appointments.models import Appointment, OutboxEvent
from goals.models import ClientGoal
from services.models import SalonService
from services.tests.test_synthetic_rows_leak_sweep import (  # noqa: F401 — засеянный набор и вызывающие
    TOKEN,
    _bot,
    bot_users,
    seeded,
)
from wellness.models import Plan, PlanStepBooking
from wellness.tests.plan_consent import ATTESTATION
from wellness.tests.test_plan_engine_steps_2868 import APPOINTMENTS_URL, RESOLUTION_URL, SAFETY, _save, _step
from wellness.tests.test_plan_step_candidates_2868 import CANDIDATES_URL

pytestmark = pytest.mark.django_db

KEY = "synthetic_event_hair"
PLAIN = "бот: обычный человек"
PERSONA = "бот: тестовая персона"


@pytest.fixture
def offer(seeded) -> SalonService:  # noqa: F811
    return SalonService.objects.get(synthetic=True)


def _slot(seeded, offer) -> str:  # noqa: F811
    """Свободное окно тест-мастера по синтетической услуге — по идентификатору, как идёт тестовый путь."""
    day = (timezone.localdate() + timedelta(days=2)).isoformat()
    response = _bot("bot:sweep-persona").get(
        f"/api/v1/internal/specialists/{seeded['master_id']}/slots/", {"service_id": str(offer.pk), "date": day},
    )
    assert response.status_code == 200, response.content[:300]
    payload = response.json()
    slots = payload.get("slots") if "slots" in payload else (payload.get("data") or {}).get("slots")
    assert slots, payload
    return slots[0]  # строка ISO-8601 с поясом мастера


def _book(user, seeded, offer, start: str, *, provenance: dict | None = None):  # noqa: F811
    body = {
        "client_id": str(user.id), "specialist_id": seeded["master_id"], "service_id": str(offer.pk),
        "start_datetime": start, "payment_required": False,
    }
    if provenance is not None:
        # Запись от шага — действие Плана: несёт утверждение основания, как остальные его ручки.
        body.update(provenance=provenance, **ATTESTATION)
    return _bot(user.username).post(
        APPOINTMENTS_URL, body, format="json", HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )


def _nothing_was_booked() -> None:
    assert not Appointment.objects.exists()
    assert not PlanStepBooking.objects.exists()
    assert not OutboxEvent.objects.exists()


def _refused_as_an_unknown_service(response) -> None:
    assert response.status_code == 422, response.content[:300]
    assert response.json()["error"]["code"] == "SERVICE_NOT_ACTIVE", response.content[:300]


def _stand_on(settings, user) -> None:
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(user.pk)]


class TestTheOrdinaryPathCannotBookASyntheticOffer:
    @pytest.mark.parametrize("who", [PLAIN, PERSONA])
    def test_knowing_the_ids_is_not_enough(self, seeded, bot_users, offer, who) -> None:  # noqa: F811
        start = _slot(seeded, offer)
        _refused_as_an_unknown_service(_book(bot_users[who], seeded, offer, start))
        _nothing_was_booked()

    def test_a_test_subject_under_the_stand_flags_cannot_either(
        self, seeded, bot_users, offer, settings,  # noqa: F811
    ) -> None:
        """Разрешение на синтетику — не разрешение записываться мимо плана."""
        persona = bot_users[PERSONA]
        _stand_on(settings, persona)
        start = _slot(seeded, offer)
        _refused_as_an_unknown_service(_book(persona, seeded, offer, start))
        _nothing_was_booked()

    def test_the_refusal_is_the_one_of_a_service_that_does_not_exist(
        self, seeded, bot_users, offer,  # noqa: F811
    ) -> None:
        start = _slot(seeded, offer)
        user = bot_users[PLAIN]
        unknown = _bot(user.username).post(
            APPOINTMENTS_URL,
            {"client_id": str(user.id), "specialist_id": seeded["master_id"], "service_id": str(uuid.uuid4()),
             "start_datetime": start, "payment_required": False},
            format="json", HTTP_X_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        synthetic = _book(user, seeded, offer, start)
        assert (synthetic.status_code, synthetic.json()) == (unknown.status_code, unknown.json())


class TestTheIsolatedTestPath:
    def _marked_plan_at_the_offer(self, settings, persona, offer) -> Plan:
        _stand_on(settings, persona)
        persona.refresh_from_db()
        plan = _save(
            ClientGoal.objects.get(client=persona), steps=[_step("s1", capability_ref=KEY, outcome_ref="event")],
        )
        assert plan.synthetic is True
        found = _bot(persona.username).post(
            CANDIDATES_URL, {"plan_id": str(plan.id), "step_id": "s1", **SAFETY, **ATTESTATION}, format="json",
        )
        assert found.status_code == 200, found.content[:400]
        data = found.json()["data"]
        assert [c["tenant_offer_ref"] for c in data["candidates"]] == [str(offer.pk)], data
        chosen = _bot(persona.username).post(
            RESOLUTION_URL,
            {"plan_id": str(plan.id), "step_id": "s1", "level": "OFFER",
             "canonical_service_ref": str(offer.template_id), "tenant_offer_ref": str(offer.pk),
             "resolver_decision_id": data["search_id"], **SAFETY, **ATTESTATION},
            format="json",
        )
        assert chosen.status_code == 201, chosen.content[:400]
        return plan

    def test_from_a_step_of_a_marked_plan_the_booking_is_made(
        self, seeded, bot_users, offer, settings,  # noqa: F811
    ) -> None:
        """Сквозной путь на засеянном наборе: план → кандидат → выбор → слот → запись."""
        persona = bot_users[PERSONA]
        plan = self._marked_plan_at_the_offer(settings, persona, offer)
        start = _slot(seeded, offer)

        response = _book(
            persona, seeded, offer, start,
            provenance={"entry_point": "PLAN_STEP", "plan_id": str(plan.id), "step_id": "s1", **SAFETY},
        )

        assert response.status_code == 201, response.content[:400]
        booking = Appointment.objects.get()
        assert str(booking.salon_service_id) == str(offer.pk)
        assert PlanStepBooking.objects.filter(appointment=booking, plan=plan, step_id="s1").count() == 1

    def test_the_same_person_without_the_step_block_is_still_refused(
        self, seeded, bot_users, offer, settings,  # noqa: F811
    ) -> None:
        """Помеченный план есть, шаг доведён до услуги — но запись без блока шага остаётся обычной."""
        persona = bot_users[PERSONA]
        self._marked_plan_at_the_offer(settings, persona, offer)
        start = _slot(seeded, offer)
        _refused_as_an_unknown_service(_book(persona, seeded, offer, start))
        assert not Appointment.objects.exists()
