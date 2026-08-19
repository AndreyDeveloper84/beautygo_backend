"""Tests for the goal layer (DRF-1190 / OD-1 / OD-2, Ответ 3).

Coverage:
- ClientGoal invariants: key-or-text CheckConstraint; one active per client
- Auth boundary of both internal endpoints (Bearer + X-External-User-ID)
- select flow: key / verbatim text / replacement / need_guidance
- decision-context document: missing kinds, clarification prompt,
  "screen computes nothing" shape property
- goals.resolution: key mapping, exact-label text match, refusal to map
- goal_selected analytics event (no verbatim text in payload)
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from goals.decision_context import (
    MISSING_GOAL,
    MISSING_GOAL_CLARIFICATION,
    MISSING_GOAL_GUIDANCE,
    build_decision_context,
)
from goals.models import ClientGoal
from goals.resolution import resolve_goal_category_ids
from services.models import GoalOption, GoalOptionCategory, ServiceCategory
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-goals"
CTX_URL = "/api/v1/internal/me/decision-context/"
SELECT_URL = "/api/v1/internal/me/goals/select/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def external_user_id():
    return "bot:goals"


@pytest.fixture
def customer(db, external_user_id):
    return User.objects.create_user(
        username=external_user_id, password="x", role="client",
        phone="+79995000001", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Массаж тела", slug="massage-body")


@pytest.fixture
def goal_option(db, category):
    option = GoalOption.objects.create(
        key="relax", label="Расслабиться и восстановиться", sort_order=10,
    )
    GoalOptionCategory.objects.create(
        goal_option=option, category=category, sort_order=0,
    )
    return option


def _api(*, bearer: str | None = VALID_TOKEN, external_user_id: str = "bot:goals"):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestClientGoalModel:
    def test_key_or_text_required(self, customer):
        with pytest.raises(IntegrityError):
            ClientGoal.objects.create(client=customer, source_channel="bot")

    def test_one_active_per_client(self, customer):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        with pytest.raises(IntegrityError):
            ClientGoal.objects.create(
                client=customer, goal_text="хочу отдохнуть", source_channel="bot",
            )

    def test_second_active_allowed_after_close(self, customer):
        first = ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        first.is_active = False
        first.save()
        ClientGoal.objects.create(
            client=customer, goal_text="хочу отдохнуть", source_channel="miniapp",
        )
        assert ClientGoal.objects.filter(client=customer).count() == 2


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuthBoundary:
    def test_decision_context_missing_bearer_denied(self, token, customer):
        r = _api(bearer=None).get(CTX_URL)
        assert r.status_code == 403

    def test_decision_context_wrong_bearer_denied(self, token, customer):
        r = _api(bearer="wrong").get(CTX_URL)
        assert r.status_code == 403

    def test_select_missing_bearer_denied(self, token, customer):
        r = _api(bearer=None).post(
            SELECT_URL, {"goal_key": "relax", "source_channel": "bot"}, format="json",
        )
        assert r.status_code == 403
        assert ClientGoal.objects.count() == 0


# ---------------------------------------------------------------------------
# Decision context document
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDecisionContext:
    def test_no_goal_missing_goal(self, customer):
        doc = build_decision_context(customer)
        assert doc["known"]["goal"] is None
        assert [m["kind"] for m in doc["missing"]] == [MISSING_GOAL]
        assert doc["missing"][0]["prompt"]

    def test_goal_with_key_no_missing(self, customer, goal_option):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        doc = build_decision_context(customer)
        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["missing"] == []

    def test_text_only_goal_needs_clarification(self, customer):
        ClientGoal.objects.create(
            client=customer, goal_text="хочу похудеть к отпуску",
            source_channel="miniapp",
        )
        doc = build_decision_context(customer)
        kinds = [m["kind"] for m in doc["missing"]]
        assert kinds == [MISSING_GOAL_CLARIFICATION]
        # OD-2: дословный текст возвращается в промпте уточнения.
        assert "хочу похудеть к отпуску" in doc["missing"][0]["prompt"]

    def test_guidance_state(self, customer):
        doc = build_decision_context(customer, guidance=True)
        assert [m["kind"] for m in doc["missing"]] == [MISSING_GOAL_GUIDANCE]

    def test_suggestions_only_active(self, customer, goal_option):
        GoalOption.objects.create(key="off", label="Скрытая", is_active=False)
        doc = build_decision_context(customer)
        assert doc["suggestions"] == [
            {"key": "relax", "label": "Расслабиться и восстановиться"},
        ]

    def test_document_shape_screen_computes_nothing(self, customer, goal_option):
        """Инвариант Ответа 3: документ содержит только отображаемое —
        ни флагов, ни сырых полей, из которых выводится другое содержимое."""
        doc = build_decision_context(customer)
        assert set(doc) == {"version", "known", "missing", "suggestions", "intents"}
        for suggestion in doc["suggestions"]:
            assert set(suggestion) == {"key", "label"}
        for item in doc["missing"]:
            assert set(item) == {"kind", "prompt"}
        for intent in doc["intents"]:
            assert set(intent) == {"id", "label"}


# ---------------------------------------------------------------------------
# Select flow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalSelect:
    def test_select_key_returns_updated_document(self, token, customer, goal_option):
        r = _api().post(
            SELECT_URL, {"goal_key": "relax", "source_channel": "miniapp"},
            format="json",
        )
        assert r.status_code == 200
        goal = ClientGoal.objects.get(client=customer, is_active=True)
        assert goal.goal_key == "relax"
        assert goal.source_channel == "miniapp"
        doc = r.json()["data"]
        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["missing"] == []

    def test_select_text_stored_verbatim(self, token, customer):
        r = _api().post(
            SELECT_URL,
            {"goal_text": "  хочу похудеть к отпуску  ", "source_channel": "bot"},
            format="json",
        )
        assert r.status_code == 200
        goal = ClientGoal.objects.get(client=customer, is_active=True)
        # OD-2: дословно (trim — только края, без нормализации).
        assert goal.goal_text == "хочу похудеть к отпуску"
        assert goal.goal_key is None
        doc = r.json()["data"]
        assert doc["missing"][0]["kind"] == MISSING_GOAL_CLARIFICATION

    def test_replacement_closes_previous(self, token, customer, goal_option):
        _api().post(
            SELECT_URL, {"goal_key": "relax", "source_channel": "bot"}, format="json",
        )
        _api().post(
            SELECT_URL, {"goal_text": "передумала", "source_channel": "bot"},
            format="json",
        )
        goals = list(ClientGoal.objects.filter(client=customer).order_by("selected_at"))
        assert len(goals) == 2
        assert goals[0].is_active is False
        assert goals[1].is_active is True

    def test_need_guidance_creates_no_goal_no_event(
        self, token, customer, goal_option,
    ):
        r = _api().post(
            SELECT_URL, {"intent": "need_guidance", "source_channel": "miniapp"},
            format="json",
        )
        assert r.status_code == 200
        assert ClientGoal.objects.count() == 0
        assert AnalyticsEvent.objects.filter(
            event_name=event_catalogue.GOAL_SELECTED,
        ).count() == 0
        doc = r.json()["data"]
        assert doc["missing"][0]["kind"] == MISSING_GOAL_GUIDANCE

    def test_exactly_one_input_required(self, token, customer):
        for body in (
            {"source_channel": "bot"},
            {"goal_key": "relax", "goal_text": "x", "source_channel": "bot"},
        ):
            r = _api().post(SELECT_URL, body, format="json")
            assert r.status_code == 400, body
        assert ClientGoal.objects.count() == 0

    def test_goal_selected_event_payload_has_no_verbatim_text(
        self, token, customer,
    ):
        _api().post(
            SELECT_URL,
            {"goal_text": "мой личный текст цели", "source_channel": "bot"},
            format="json",
        )
        event = AnalyticsEvent.objects.get(
            event_name=event_catalogue.GOAL_SELECTED,
        )
        assert event.actor_id == customer.id
        assert event.payload["goal_key"] is None
        assert event.payload["has_text"] is True
        assert event.payload["source_channel"] == "bot"
        assert "мой личный текст цели" not in str(event.payload)


# ---------------------------------------------------------------------------
# Resolution (goal -> category ids)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResolution:
    def test_no_goal_returns_none(self, customer):
        assert resolve_goal_category_ids(customer) is None

    def test_key_resolves_to_categories(self, customer, goal_option, category):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        assert resolve_goal_category_ids(customer) == [category.id]

    def test_unknown_key_returns_none(self, customer):
        ClientGoal.objects.create(
            client=customer, goal_key="ghost", source_channel="bot",
        )
        assert resolve_goal_category_ids(customer) is None

    def test_text_exact_label_match_resolves(self, customer, goal_option, category):
        ClientGoal.objects.create(
            client=customer, goal_text="  Расслабиться И Восстановиться ",
            source_channel="bot",
        )
        # trim делается при записи через API; здесь создаём напрямую —
        # резолвер сам trim'ит перед сравнением.
        assert resolve_goal_category_ids(customer) == [category.id]

    def test_text_free_phrase_refused(self, customer, goal_option):
        """OD-1: низкая уверенность — уточнить, а не маппить насильно."""
        ClientGoal.objects.create(
            client=customer, goal_text="хочу похудеть к отпуску",
            source_channel="bot",
        )
        assert resolve_goal_category_ids(customer) is None
