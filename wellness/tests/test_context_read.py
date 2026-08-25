"""Приёмка DRF-1344 — internal wellness-context read для решающего слоя.

Доказывает:
- auth boundary: 403 без валидного Bearer / X-External-User-ID
  (по образцу goals/tests/test_goal_layer.py);
- fail-closed: пока гейты закрыты (а они закрыты — Gate D до scope
  `goal_memory`, Gate O до Privacy/Legal), документ честно gated, коды
  причин берутся из самих гейтов, не хардкод;
- структурный запрет на значения: при полных ORM-строках (outcome,
  plan, link, наблюдение value_numeric=96) в payload нет ни значения
  наблюдения, ни desired_state — только коды; отрицательным
  утверждениям — положительная стража на тех же данных
  (progress_state == derived присутствует);
- horizon_status: none / upcoming / elapsed по target_date
  (относительные даты, никаких констант);
- закрытые результаты в документ не попадают.

Открытая ветка (гейты открыты) сегодня недостижима через реальные гейты —
она проверяется monkeypatch'ем гейтов в точке импорта модуля; это тест
формы будущего документа, а не ослабление гейтов (их fail-closed
доказывает wellness/tests/test_fail_closed.py на реальных функциях).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from wellness.models import (
    DesiredOutcome,
    EvidenceRegistryEntry,
    PersonalPlan,
    PlanOutcomeLink,
    ProgressObservation,
)
from wellness.services import (
    GateDecision,
    Purpose,
    body_observation_gate,
    goal_intention_gate,
)

VALID_TOKEN = "test-ayla-internal-token-wellness"
CTX_URL = "/api/v1/internal/me/wellness-context/"

WEIGHT_KG_1 = Decimal("96.0")
WEIGHT_KG_2 = Decimal("94.0")
DESIRED_STATE = Decimal("90.0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id():
    return "bot:wellness-context"


@pytest.fixture
def customer(db, external_user_id):
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79995000044", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def open_gates(monkeypatch):
    """Открытые гейты — форма будущего документа (гейты откроются не в
    этой задаче). Патчится точка импорта в wellness.context_read."""
    allow = GateDecision(allowed=True, reason_code="allowed")
    monkeypatch.setattr(
        "wellness.context_read.goal_intention_gate",
        lambda attestation, purpose: allow,
    )
    monkeypatch.setattr(
        "wellness.context_read.body_observation_gate",
        lambda attestation, purpose: allow,
    )


@pytest.fixture
def registry_weight(db):
    return EvidenceRegistryEntry.objects.create(
        outcome_target="body_weight",
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        origin=ProgressObservation.Origin.USER_STATED,
        instrument="",
        approved_by="owner",
        approved_at=timezone.now(),
    )


@pytest.fixture
def outcome(db, customer, registry_weight):
    outcome = DesiredOutcome.objects.create(
        user=customer, target="body_weight",
        statement_text="хочу сбросить вес",
        direction=DesiredOutcome.Direction.REDUCE,
        desired_state_numeric=DESIRED_STATE,
    )
    # amendment A: ряд наблюдений не может быть раньше outcome.created_at.
    DesiredOutcome.objects.filter(pk=outcome.pk).update(
        created_at=timezone.now() - timedelta(days=31),
    )
    outcome.refresh_from_db()
    return outcome


@pytest.fixture
def plan(db, customer):
    return PersonalPlan.objects.create(user=customer)


@pytest.fixture
def link(db, plan, outcome):
    return PlanOutcomeLink.objects.create(plan=plan, outcome=outcome)


def _weight_row(user, value, *, days_ago=0):
    return ProgressObservation.objects.create(
        user=user,
        observation_type=ProgressObservation.ObservationType.WEIGHT,
        value_numeric=value,
        observed_at=timezone.now() - timedelta(days=days_ago),
    )


def _api(*, bearer=VALID_TOKEN, external_user_id="bot:wellness-context"):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthBoundary:
    def test_missing_bearer_denied(self, token, customer):
        assert _api(bearer=None).get(CTX_URL).status_code == 403

    def test_wrong_bearer_denied(self, token, customer):
        assert _api(bearer="wrong").get(CTX_URL).status_code == 403

    def test_missing_external_user_id_denied(self, token, customer):
        assert _api(external_user_id=None).get(CTX_URL).status_code == 403


# ---------------------------------------------------------------------------
# Fail-closed: гейты закрыты (всегда, сегодня)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGatedDocument:
    def test_document_is_gated_with_gate_reason_codes(self, token, customer):
        """Коды причин — из самих гейтов при purpose=processing, не хардкод."""
        r = _api().get(CTX_URL)
        assert r.status_code == 200
        assert r.json()["data"] == {
            "plan": None,
            "outcomes": [],
            "gated": {
                "gate_d": goal_intention_gate(None, Purpose.PROCESSING).reason_code,
                "gate_o": body_observation_gate(None, Purpose.PROCESSING).reason_code,
            },
        }

    def test_existing_rows_do_not_open_the_document(
        self, token, customer, outcome, plan, link,
    ):
        """Grep-тест при закрытых гейтах: данные в БД есть — документ gated."""
        _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        r = _api().get(CTX_URL)
        assert r.status_code == 200
        doc = r.json()["data"]
        assert doc["plan"] is None
        assert doc["outcomes"] == []
        assert doc["gated"]["gate_d"] and doc["gated"]["gate_o"]
        payload = r.content.decode()
        assert "96" not in payload
        assert "90" not in payload


# ---------------------------------------------------------------------------
# Открытая ветка (будущее): форма документа — только коды
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOpenDocument:
    def test_no_plan_no_outcomes(self, token, customer, open_gates):
        doc = _api().get(CTX_URL).json()["data"]
        assert doc == {"plan": None, "outcomes": [], "gated": None}

    def test_full_projection_codes_only(
        self, token, customer, outcome, plan, link, open_gates,
    ):
        """Grep-тест из задачи: значения наблюдения (96) и желаемого
        состояния (90) в payload нет; progress_state есть и корректен."""
        _weight_row(customer, WEIGHT_KG_1, days_ago=14)
        _weight_row(customer, WEIGHT_KG_2, days_ago=1)
        r = _api().get(CTX_URL)
        assert r.status_code == 200
        doc = r.json()["data"]

        # Положительная стража на тех же данных: коды на месте.
        assert doc["plan"] == {"status": PersonalPlan.Status.ACTIVE}
        assert doc["outcomes"] == [{
            "target": "body_weight",
            "link_status": PlanOutcomeLink.Status.ACTIVE,
            "horizon_status": PlanOutcomeLink.HorizonStatus.NONE,
            "progress_state": "derived",
        }]
        assert doc["gated"] is None

        # Отрицательное утверждение: ни одного значения — структурно.
        payload = r.content.decode()
        assert "96" not in payload
        assert "94" not in payload
        assert "90" not in payload
        assert "хочу сбросить вес" not in payload

    def test_outcome_without_link_has_null_link_fields(
        self, token, customer, outcome, open_gates,
    ):
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["outcomes"][0]["link_status"] is None
        assert doc["outcomes"][0]["horizon_status"] is None
        assert doc["outcomes"][0]["progress_state"] == "no_observations"

    def test_closed_outcome_is_excluded(
        self, token, customer, outcome, open_gates,
    ):
        outcome.status = DesiredOutcome.Status.CLOSED_BY_USER
        outcome.closed_at = timezone.now()
        outcome.save()
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["outcomes"] == []

    @pytest.mark.parametrize(
        "delta_days,expected",
        [
            (None, PlanOutcomeLink.HorizonStatus.NONE),
            (1, PlanOutcomeLink.HorizonStatus.UPCOMING),
            (-1, PlanOutcomeLink.HorizonStatus.ELAPSED),
        ],
    )
    def test_horizon_status_by_target_date(
        self, token, customer, outcome, plan, open_gates, delta_days, expected,
    ):
        target_date = (
            None if delta_days is None
            else timezone.localdate() + timedelta(days=delta_days)
        )
        PlanOutcomeLink.objects.create(
            plan=plan, outcome=outcome, target_date=target_date,
        )
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["outcomes"][0]["horizon_status"] == expected
