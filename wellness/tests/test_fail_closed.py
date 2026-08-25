"""Fail-closed доказуем, не заявлен (PROPOSAL_GOALS_MODEL_FINAL §7.3).

Постоянная проверка: запись body observation без attestation отказана;
валидная attestation её НЕ открывает (trusted source не подключён);
запись намерения до утверждения `goal_memory` отказана; boolean/self-
declared «consent» невозможен контрактно (amendment E) — в сигнатурах
публичных writers нет параметра `consent`; путь прав субъекта не
блокируется даже без attestation (amendment C).

Этот тест ДОЛЖЕН краснеть при любом ослаблении гейтов.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest

from users.models import User
from wellness.models import (
    DesiredOutcome,
    ProgressObservation,
)
from wellness.services import (
    ConsentAttestation,
    Purpose,
    body_observation_gate,
    goal_intention_gate,
    record_observation,
    record_outcome,
)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:wellness", password="x", role="client",
        phone="+79995000010", is_proxy=True,
    )


def _attestation() -> ConsentAttestation:
    """Полностью валидная по форме attestation — и её недостаточно."""
    return ConsentAttestation(
        scope="goal_memory",
        authority="user",
        provenance="user_stated",
        document_version="consent-v1",
        captured_at=datetime(2026, 8, 25, tzinfo=dt_timezone.utc),
    )


# ---------------------------------------------------------------------------
# Запись отказана — с attestation и без
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWritersRefuse:
    def test_observation_without_attestation_refused(self, customer):
        decision = record_observation(
            customer,
            observation_type=ProgressObservation.ObservationType.WEIGHT,
            value_numeric=Decimal("70.0"),
        )
        assert decision.allowed is False
        assert decision.reason_code == "blocked_pending_privacy_legal"
        assert ProgressObservation.objects.count() == 0

    def test_observation_with_valid_attestation_still_refused(self, customer):
        """Gate O: trusted source не подключён — валидная attestation
        не открывает запись (GOALS-R5, amendment E)."""
        decision = record_observation(
            customer,
            observation_type=ProgressObservation.ObservationType.SELF_ASSESSMENT,
            value_ordinal=2,
            instrument=ProgressObservation.INSTRUMENT_NOTICEABILITY_0_3_V1,
            attestation=_attestation(),
        )
        assert decision.allowed is False
        assert ProgressObservation.objects.count() == 0

    def test_outcome_write_refused_before_goal_memory_scope(self, customer):
        """Gate D: scope `goal_memory` не утверждён (GOALS-R6)."""
        for attestation in (None, _attestation()):
            decision = record_outcome(
                customer,
                target="body_weight",
                statement_text="хочу весить 65 кг",
                attestation=attestation,
            )
            assert decision.allowed is False
            assert decision.reason_code == "scope_not_approved"
        assert DesiredOutcome.objects.count() == 0


# ---------------------------------------------------------------------------
# Контракт сигнатур (amendment E): никакого consent=True/False
# ---------------------------------------------------------------------------


class TestNoBooleanConsentContract:
    @pytest.mark.parametrize(
        "writer",
        [record_outcome, record_observation, goal_intention_gate, body_observation_gate],
    )
    def test_no_consent_parameter_in_signature(self, writer):
        assert "consent" not in inspect.signature(writer).parameters

    def test_attestation_is_typed(self):
        """Поле attestation типизировано ConsentAttestation, не bool."""
        for writer in (record_outcome, record_observation):
            param = inspect.signature(writer).parameters["attestation"]
            assert "ConsentAttestation" in str(param.annotation)


# ---------------------------------------------------------------------------
# Права субъекта не блокируются (amendment C)
# ---------------------------------------------------------------------------


class TestSubjectRightsPass:
    def test_body_gate_subject_rights_allowed_without_attestation(self):
        """inspect/export/correction/deletion — в том числе после revoke."""
        decision = body_observation_gate(None, purpose=Purpose.SUBJECT_RIGHTS)
        assert decision.allowed is True

    def test_intention_gate_subject_rights_allowed_without_attestation(self):
        decision = goal_intention_gate(None, purpose=Purpose.SUBJECT_RIGHTS)
        assert decision.allowed is True

    def test_unknown_purpose_refused(self):
        """Fail-closed: неизвестный purpose — отказ, не пропуск."""
        assert body_observation_gate(_attestation(), purpose="nope").allowed is False
        assert goal_intention_gate(_attestation(), purpose="nope").allowed is False
