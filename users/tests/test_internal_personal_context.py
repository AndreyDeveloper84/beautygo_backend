"""Tests для internal Bearer personal-context API (A1a).

Контракт: PERSONAL_CONTEXT_INTERNAL_API_SPEC. Bearer <AYLA_INTERNAL_API_TOKEN>,
ключ = ayla_user_id. Green-зона, lazy-create, ask-eligibility (обёртка 8 правил).
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from users.models import User, UserPersonalContext

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-internal-token"  # noqa: S105 — test constant, not a real secret


@pytest.fixture(autouse=True)
def _set_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def user() -> User:
    return User.objects.create_user(
        username="pc_bot_owner", password="x", role="client", phone="+79995550001",
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _url(user_id, suffix: str = "") -> str:
    return f"/api/v1/internal/users/{user_id}/personal-context/{suffix}"


class TestAuth:
    def test_missing_bearer_denied(self, user):
        resp = _api(bearer=None).get(_url(user.id))
        assert resp.status_code in (401, 403)

    def test_wrong_bearer_denied(self, user):
        resp = _api(bearer="nope").get(_url(user.id))
        assert resp.status_code in (401, 403)


class TestGet:
    def test_empty_context_lazy_create_200(self, user):
        resp = _api().get(_url(user.id))
        assert resp.status_code == 200
        data = resp.data["data"]
        assert data["ayla_user_id"] == str(user.id)
        assert "preferred_time_slots" in data["context"]
        assert data["meta"]["filled_fields"] == 0
        # lazy-create — строка появилась.
        assert UserPersonalContext.objects.filter(user=user).exists()

    def test_unknown_user_404(self):
        resp = _api().get(_url(uuid.uuid4()))
        assert resp.status_code == 404
        assert resp.data["error"]["code"] == "USER_NOT_FOUND"


class TestPatch:
    def test_sets_field_and_source(self, user):
        resp = _api().patch(
            _url(user.id),
            {"updates": [{"field": "preferred_time_slots",
                          "value": ["evening"], "source": "explicit"}]},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["data"]["context"]["preferred_time_slots"] == ["evening"]
        ctx = UserPersonalContext.objects.get(user=user)
        assert ctx.preferred_time_slots == ["evening"]
        assert ctx.data_sources["preferred_time_slots"] == "explicit"

    def test_batch_over_limit_400(self, user):
        updates = [{"field": "diet_type", "value": "x"} for _ in range(11)]
        resp = _api().patch(_url(user.id), {"updates": updates}, format="json")
        assert resp.status_code == 400
        assert resp.data["error"]["code"] == "TOO_MANY_FIELDS"

    def test_empty_updates_400(self, user):
        resp = _api().patch(_url(user.id), {"updates": []}, format="json")
        assert resp.status_code == 400

    def test_unknown_field_rejected(self, user):
        resp = _api().patch(
            _url(user.id),
            {"updates": [{"field": "pregnancy", "value": True}]},
            format="json",
        )
        assert resp.status_code == 400


class TestAskEligibility:
    def test_blocked_before_onboarding(self, user):
        # onboarding_completed=False по умолчанию → first_interaction.
        resp = _api().get(_url(user.id, "ask-eligibility/"))
        assert resp.status_code == 200
        assert resp.data["data"]["should_ask"] is False
        assert resp.data["data"]["blocked_by"] == "first_interaction"

    def test_returns_field_after_onboarding(self, user):
        user.onboarding_completed = True
        user.save(update_fields=["onboarding_completed"])
        resp = _api().get(_url(user.id, "ask-eligibility/"))
        assert resp.status_code == 200
        body = resp.data["data"]
        assert body["should_ask"] is True
        assert body["field"] == "preferred_time_slots"  # первый кандидат
        assert body["prompt_hint"]


class TestMarkAskedAndSkip:
    def test_mark_asked_sets_cooldown(self, user):
        resp = _api().post(
            _url(user.id, "mark-asked/"), {"field": "preferred_time_slots"}, format="json",
        )
        assert resp.status_code == 200
        ctx = UserPersonalContext.objects.get(user=user)
        assert "preferred_time_slots" in (ctx.last_asked_at or {})

    def test_skip_increments_count(self, user):
        resp = _api().post(
            _url(user.id, "skip/"), {"field": "preferred_time_slots"}, format="json",
        )
        assert resp.status_code == 200
        assert resp.data["data"]["skip_count"] == 1
