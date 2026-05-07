"""DRF-267 — internal /insights/cross_domain/ endpoints.

Five service-to-service routes (X-Service-Token + X-External-User-ID):

- GET    /internal/insights/cross_domain/                  — top-1 rec
- POST   /internal/insights/cross_domain/seen/{shown_id}/      — confirm
- POST   /internal/insights/cross_domain/dismiss/{shown_id}/   — skip / pause
- POST   /internal/insights/cross_domain/convert/{shown_id}/   — booking
- GET    /internal/insights/cross_domain/history/?limit=20    — transparency UI

GET reuses CrossDomainEngine. /seen/ /dismiss/ /convert/ are simple
state transitions on CrossDomainShownRule — auth via service-account
token, external_user_id resolves to the row's owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from nutrition.models import (
    CrossDomainRule,
    CrossDomainShownRule,
    NutritionProfile,
)
from nutrition.services.pattern_detection_service import (
    DetectedPattern,
    PatternDetectionResult,
)


User = get_user_model()


@pytest.fixture
def cd_endpoint_user(db, settings):
    settings.NUTRITION_SERVICE_TOKEN = "test-token"
    # Username must be `bot:NNN` so resolve_external_user can find this
    # user via X-External-User-ID header (it does username lookup).
    user = User.objects.create_user(
        username="bot:9001", role="client", is_proxy=True,
    )
    NutritionProfile.objects.create(
        user=user, gender="female", age=40,
        height_cm=165, weight_kg=70.0,
        goal="maintain", pace="moderate",
    )
    return user


@pytest.fixture
def vitamin_d_rule(db):
    return CrossDomainRule.objects.create(
        rule_id="vit_d_to_argan_ep",
        nutrition_trigger="low_vitamin_d",
        service_category_slug="massage-argan-oil",
        insight_text_template="text",
        rationale_text="rationale",
        disclaimer_text="disclaimer",
        is_active=True, legal_reviewed=True,
    )


def _service_client(user):
    """APIClient with X-Service-Token + X-External-User-ID for `user`."""
    client = APIClient()
    # external_user_id matches user.username (resolve_external_user
    # does username lookup). Tests fixture sets username=bot:9001.
    client.defaults.update({
        "HTTP_X_SERVICE_TOKEN": "test-token",
        "HTTP_X_EXTERNAL_USER_ID": user.username,
    })
    return client


def _patch_engine_returns_recommendation():
    """Stub detect_patterns to fire low_vitamin_d + supply check OK."""
    pattern = DetectedPattern(
        slug="low_vitamin_d", name_ru="x", count=5,
        active_window_days=7, severity="medium",
        recent_dates=[], advice_template_args={},
        display_hint="primary",
    )
    return [
        patch(
            "nutrition.services.cross_domain_engine.detect_patterns",
            return_value=PatternDetectionResult(
                active_days=10, patterns=[pattern],
            ),
        ),
        patch(
            "nutrition.services.cross_domain_engine.CrossDomainEngine."
            "_has_supply",
            return_value=True,
        ),
    ]


@pytest.mark.django_db
class TestGetCrossDomainInsight:
    """GET /internal/insights/cross_domain/ → top-1 or null."""

    def test_returns_recommendation_when_engine_yields(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        ctxs = _patch_engine_returns_recommendation()
        for c in ctxs:
            c.__enter__()
        try:
            client = _service_client(cd_endpoint_user)
            resp = client.get(
                "/api/v1/nutrition/internal/insights/cross_domain/",
            )
        finally:
            for c in ctxs:
                c.__exit__(None, None, None)

        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        # LB-10: nested envelope matches maxbot NutritionClient parser.
        assert body["has_insight"] is True
        insight = body["insight"]
        assert insight["rule_slug"] == "vit_d_to_argan_ep"
        assert insight["nutrition_trigger"] == "low_vitamin_d"
        assert "shown_id" in insight
        assert insight["data_points"] == 5

    def test_returns_no_recommendation_when_engine_yields_none(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        # No active patterns → engine returns None.
        with patch(
            "nutrition.services.cross_domain_engine.detect_patterns",
            return_value=PatternDetectionResult(active_days=0, patterns=[]),
        ):
            client = _service_client(cd_endpoint_user)
            resp = client.get(
                "/api/v1/nutrition/internal/insights/cross_domain/",
            )
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        # LB-10: nested envelope; flag is has_insight, no `insight` key.
        assert body["has_insight"] is False
        assert "insight" not in body

    def test_unauthorized_without_service_token(
        self, cd_endpoint_user,
    ):
        client = APIClient()
        resp = client.get(
            "/api/v1/nutrition/internal/insights/cross_domain/",
        )
        # IsServiceAccount permission rejects.
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestPostSeen:
    """POST /seen/{id}/ — confirm rendering, activate cooldown."""

    def test_seen_updates_seen_at(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        assert shown.seen_at is None

        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"seen/{shown.id}/",
        )
        assert resp.status_code == 200, resp.content
        shown.refresh_from_db()
        assert shown.seen_at is not None

    def test_seen_idempotent(self, cd_endpoint_user, vitamin_d_rule):
        # Calling /seen/ twice should not bump seen_at the second time.
        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"seen/{shown.id}/",
        )
        first_seen = CrossDomainShownRule.objects.get(id=shown.id).seen_at

        client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"seen/{shown.id}/",
        )
        second_seen = CrossDomainShownRule.objects.get(id=shown.id).seen_at
        assert first_seen == second_seen

    def test_seen_404_for_unknown_id(self, cd_endpoint_user):
        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"seen/{uuid4()}/",
        )
        assert resp.status_code == 404

    def test_seen_404_for_other_users_row(
        self, cd_endpoint_user, vitamin_d_rule, db,
    ):
        # Security: user A can't see/dismiss user B's rows.
        other = User.objects.create_user(
            username="bot:9099", role="client", is_proxy=True,
        )
        shown = CrossDomainShownRule.objects.create(
            user=other, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)  # auth as cd_endpoint_user
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"seen/{shown.id}/",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestPostDismiss:
    """POST /dismiss/{id}/ — body {action: dismissed|paused_7d}."""

    def test_dismiss_sets_user_action(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"dismiss/{shown.id}/",
            {"action": "dismissed"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        shown.refresh_from_db()
        assert shown.user_action == "dismissed"

    def test_paused_7d_action(self, cd_endpoint_user, vitamin_d_rule):
        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"dismiss/{shown.id}/",
            {"action": "paused_7d"},
            format="json",
        )
        assert resp.status_code == 200
        shown.refresh_from_db()
        assert shown.user_action == "paused_7d"

    def test_invalid_action_400(self, cd_endpoint_user, vitamin_d_rule):
        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"dismiss/{shown.id}/",
            {"action": "bogus"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestPostConvert:
    """POST /convert/{id}/ — body {appointment_id} for attribution."""

    def test_convert_sets_user_action_and_attribution(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        from appointments.models import Appointment
        from services.models import Service, ServiceCategory
        from users.models import User as UserModel  # type: ignore
        # Build minimal appointment for FK target.
        cat = ServiceCategory.objects.create(name="Test", slug="test-cat")
        specialist_user = UserModel.objects.create_user(
            username="sp_cd", password="x", role="specialist",
        )
        # Specialist profile auto-created via signal — use the existing profile.
        sp_profile = specialist_user.specialist_profile
        sp_profile.display_name = "Test Master"
        sp_profile.status = "active"
        sp_profile.address = "Test"
        sp_profile.save()
        service = Service.objects.create(
            specialist=sp_profile, category=cat,
            name="Test Service", price=1000, duration_minutes=60,
        )
        appt = Appointment.objects.create(
            client=cd_endpoint_user, specialist=sp_profile, service=service,
            start_datetime=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
            price=1000, status="pending",
        )

        shown = CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        resp = client.post(
            f"/api/v1/nutrition/internal/insights/cross_domain/"
            f"convert/{shown.id}/",
            {"appointment_id": str(appt.id)},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        shown.refresh_from_db()
        assert shown.user_action == "converted"
        assert shown.appointment_id == appt.id


@pytest.mark.django_db
class TestGetHistory:
    """GET /history/?limit=20 — transparency UI."""

    def test_history_returns_recent_first(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        # Create 3 rows ordered oldest → newest.
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for d in range(3):
            CrossDomainShownRule.objects.create(
                user=cd_endpoint_user, rule=vitamin_d_rule,
                nutrition_trigger="low_vitamin_d",
                service_category_slug="massage-argan-oil",
                shown_at=now - timedelta(days=d),
                surface="bot",
            )

        client = _service_client(cd_endpoint_user)
        resp = client.get(
            "/api/v1/nutrition/internal/insights/cross_domain/history/",
        )
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        assert len(body["history"]) == 3
        # Recent first.
        timestamps = [item["shown_at"] for item in body["history"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_history_limit_parameter(
        self, cd_endpoint_user, vitamin_d_rule,
    ):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for d in range(5):
            CrossDomainShownRule.objects.create(
                user=cd_endpoint_user, rule=vitamin_d_rule,
                nutrition_trigger="low_vitamin_d",
                service_category_slug="massage-argan-oil",
                shown_at=now - timedelta(days=d),
                surface="bot",
            )

        client = _service_client(cd_endpoint_user)
        resp = client.get(
            "/api/v1/nutrition/internal/insights/cross_domain/history/?limit=2",
        )
        assert resp.status_code == 200
        body = resp.json().get("data", resp.json())
        assert len(body["history"]) == 2

    def test_history_only_own_rows(
        self, cd_endpoint_user, vitamin_d_rule, db,
    ):
        # Other user's rows must not leak.
        other = User.objects.create_user(
            username="bot:9300", role="client", is_proxy=True,
        )
        CrossDomainShownRule.objects.create(
            user=other, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        CrossDomainShownRule.objects.create(
            user=cd_endpoint_user, rule=vitamin_d_rule,
            nutrition_trigger="low_vitamin_d",
            service_category_slug="massage-argan-oil",
            shown_at=datetime.now(timezone.utc), surface="bot",
        )
        client = _service_client(cd_endpoint_user)
        resp = client.get(
            "/api/v1/nutrition/internal/insights/cross_domain/history/",
        )
        body = resp.json().get("data", resp.json())
        assert len(body["history"]) == 1
