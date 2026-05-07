"""DRF-230 PR 3 — PersonalizationEngine + analytics emission tests.

Coverage:
- 8 anti-spam rules through ``should_ask_question``
- ``mark_asked`` updates last_asked_at idempotently
- ``mark_skipped`` increments counter
- PATCH endpoint stamps data_sources=explicit + emits answered event
- POST /skip/ delegates to engine + emits skipped event
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model

from analytics import event_catalogue
from analytics.models import AnalyticsEvent
from rest_framework.test import APIClient

from users.models import UserPersonalContext
from users.personalization_engine import (
    COOLDOWN_HOURS,
    DOUBLE_SKIP_PAUSE_DAYS,
    SKIP_THRESHOLD_COUNT,
    mark_asked,
    mark_skipped,
    should_ask_question,
)


User = get_user_model()


PC_URL = "/api/v1/auth/users/me/personal-context/"
PC_SKIP_URL = "/api/v1/auth/users/me/personal-context/skip/"


def _now_iso(delta_hours: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


@pytest.fixture
def onboarded_user(db):
    return User.objects.create_user(
        username="pe_owner", password="x",
        role="client", phone="+79991230002",
        onboarding_completed=True,
    )


@pytest.fixture
def auth_client(onboarded_user):
    api = APIClient(HTTP_X_APP_TYPE="client")
    api.force_authenticate(user=onboarded_user)
    return api


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestShouldAskQuestion:
    def test_first_interaction_blocks(self, db):
        guest = User.objects.create_user(
            username="not_onboarded", password="x",
            role="client", phone="+79991235555",
            onboarding_completed=False,
        )
        v = should_ask_question(guest, "workplace_district")
        assert not v.allowed
        assert v.reason == "first_interaction"

    def test_no_context_yet_allows(self, onboarded_user):
        # Onboarding done, no context row yet → ask away.
        v = should_ask_question(onboarded_user, "workplace_district")
        assert v.allowed
        assert v.reason == "ok"

    def test_already_have_explicit_blocks(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            workplace_district="Арбатская",
            data_sources={"workplace_district": "explicit"},
        )
        v = should_ask_question(onboarded_user, "workplace_district")
        assert not v.allowed
        assert v.reason == "already_have_data"

    def test_already_have_inferred_blocks(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            favorite_masters=["uuid-x"],
            data_sources={"favorite_masters": "inferred"},
        )
        v = should_ask_question(onboarded_user, "favorite_masters")
        assert not v.allowed
        assert v.reason == "already_have_data"

    def test_cooldown_blocks_within_24h(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            last_asked_at={
                "workplace_district": _now_iso(delta_hours=-1),  # 1h ago
            },
        )
        v = should_ask_question(onboarded_user, "workplace_district")
        assert not v.allowed
        assert v.reason == "cooldown_24h"

    def test_cooldown_lifts_after_24h(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            last_asked_at={
                "workplace_district": _now_iso(
                    delta_hours=-(COOLDOWN_HOURS + 1),
                ),
            },
        )
        v = should_ask_question(onboarded_user, "workplace_district")
        assert v.allowed

    def test_double_skip_pause_blocks(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            skipped_questions={
                "workplace_district": {
                    "count": SKIP_THRESHOLD_COUNT,
                    "last_at": _now_iso(delta_hours=-24),  # yesterday
                },
            },
        )
        v = should_ask_question(onboarded_user, "workplace_district")
        assert not v.allowed
        assert v.reason == "double_skip_pause"

    def test_double_skip_pause_lifts_after_30_days(self, onboarded_user):
        UserPersonalContext.objects.create(
            user=onboarded_user,
            skipped_questions={
                "workplace_district": {
                    "count": SKIP_THRESHOLD_COUNT,
                    "last_at": _now_iso(
                        delta_hours=-(DOUBLE_SKIP_PAUSE_DAYS + 1) * 24,
                    ),
                },
            },
        )
        v = should_ask_question(onboarded_user, "workplace_district")
        assert v.allowed


@pytest.mark.django_db
class TestMarkAsked:
    def test_stamps_last_asked_at(self, onboarded_user):
        mark_asked(onboarded_user, "workplace_district")
        ctx = UserPersonalContext.objects.get(user=onboarded_user)
        assert "workplace_district" in ctx.last_asked_at

    def test_idempotent_overwrites(self, onboarded_user):
        mark_asked(onboarded_user, "workplace_district")
        ctx = UserPersonalContext.objects.get(user=onboarded_user)
        first = ctx.last_asked_at["workplace_district"]
        mark_asked(onboarded_user, "workplace_district")
        ctx.refresh_from_db()
        second = ctx.last_asked_at["workplace_district"]
        assert second >= first  # monotonic; equal if within 1us is fine


@pytest.mark.django_db
class TestMarkSkipped:
    def test_increments_count(self, onboarded_user):
        c1 = mark_skipped(onboarded_user, "workplace_district")
        c2 = mark_skipped(onboarded_user, "workplace_district")
        assert c1 == 1
        assert c2 == 2


# ---------------------------------------------------------------------------
# Endpoint hooks
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPatchEmitsAnsweredAndStampsExplicit:
    def test_patch_records_provenance_and_emits(self, auth_client, onboarded_user):
        resp = auth_client.patch(
            PC_URL,
            {"workplace_district": "Арбатская", "home_district": "Сокольники"},
            format="json",
        )
        assert resp.status_code == 200, resp.content

        ctx = UserPersonalContext.objects.get(user=onboarded_user)
        assert ctx.data_sources["workplace_district"] == "explicit"
        assert ctx.data_sources["home_district"] == "explicit"

        events = AnalyticsEvent.objects.filter(
            event_name=event_catalogue.PROFILE_QUESTION_ANSWERED,
            actor=onboarded_user,
        ).values_list("payload", flat=True)
        fields_seen = {p["field"] for p in events}
        assert {"workplace_district", "home_district"}.issubset(fields_seen)


@pytest.mark.django_db
class TestSkipEndpointEmits:
    def test_skip_emits_event(self, auth_client, onboarded_user):
        resp = auth_client.post(
            PC_SKIP_URL, {"field": "workplace_district"}, format="json",
        )
        assert resp.status_code == 200, resp.content

        events = AnalyticsEvent.objects.filter(
            event_name=event_catalogue.PROFILE_QUESTION_SKIPPED,
            actor=onboarded_user,
        )
        assert events.count() == 1
        payload = events.first().payload
        assert payload["field"] == "workplace_district"
        assert payload["skip_count"] == 1
