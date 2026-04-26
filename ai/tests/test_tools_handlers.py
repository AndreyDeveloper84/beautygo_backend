"""Unit tests for tool_handlers — pure validation + shaping logic."""
from __future__ import annotations

import uuid as _uuid

import pytest

from ai.application.services.specialist_context_builder import (
    SpecialistCandidate,
    SpecialistContext,
)
from ai.tools import ActionType
from ai.tools_handlers import (
    handle_ask_clarification,
    handle_confirm_booking,
    handle_show_appointments,
    handle_show_specialists,
)


pytestmark = pytest.mark.django_db


def _candidate(name="Анна"):
    return SpecialistCandidate(
        id=_uuid.uuid4(),
        display_name=name,
        rating=4.8,
        reviews_count=20,
        address="Penza",
        distance_km=None,
        services_preview=["Маникюр"],
    )


class TestShowSpecialists:
    def test_drops_invalid_ids_silently(self):
        good = _candidate()
        ctx = SpecialistContext(candidates=[good])
        bogus = _uuid.uuid4()
        result = handle_show_specialists(
            {
                "specialist_ids": [str(good.id), str(bogus)],
                "match_scores": [90, 50],
                "match_reasons": [["A"], ["B"]],
                "explanation": "test",
            },
            ctx,
        )
        assert result.action_type == ActionType.SHOW_SPECIALISTS
        ids = [s["specialist"]["id"] for s in result.action_data["specialists"]]
        assert str(good.id) in ids
        assert str(bogus) not in ids

    def test_no_valid_ids_falls_back_to_clarification(self):
        ctx = SpecialistContext(candidates=[_candidate()])
        result = handle_show_specialists(
            {
                "specialist_ids": [str(_uuid.uuid4())],
                "explanation": "test",
            },
            ctx,
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION

    def test_empty_ids_falls_back(self):
        ctx = SpecialistContext(candidates=[_candidate()])
        result = handle_show_specialists(
            {"specialist_ids": [], "explanation": ""}, ctx
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION


class TestConfirmBooking:
    def test_invalid_datetime_falls_back(self):
        result = handle_confirm_booking(
            {
                "specialist_id": str(_uuid.uuid4()),
                "service_id": str(_uuid.uuid4()),
                "datetime": "not-a-date",
            }
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION

    def test_missing_specialist_falls_back(self):
        result = handle_confirm_booking(
            {
                "specialist_id": str(_uuid.uuid4()),  # not in DB
                "service_id": str(_uuid.uuid4()),
                "datetime": "2026-06-01T14:00:00Z",
            }
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION


class TestShowAppointments:
    def test_returns_only_user_appointments(self, client_user):
        result = handle_show_appointments(
            {"filter": "upcoming"}, client_id=client_user.id
        )
        assert result.action_type == ActionType.SHOW_APPOINTMENTS
        assert "appointments" in result.action_data
        assert isinstance(result.action_data["appointments"], list)


class TestAskClarification:
    def test_passes_through_question_and_options(self):
        result = handle_ask_clarification(
            {"question": "когда удобно?", "options": ["утро", "вечер"]}
        )
        assert result.action_type == ActionType.ASK_CLARIFICATION
        assert result.action_data["question"] == "когда удобно?"
        assert result.action_data["options"] == ["утро", "вечер"]

    def test_no_question_falls_back(self):
        result = handle_ask_clarification({"question": ""})
        assert result.action_type == ActionType.ASK_CLARIFICATION
        # Different fallback content (autodef question text) but same type.
        assert "question" in result.action_data
