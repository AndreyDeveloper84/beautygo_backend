"""Ориентир калорий в сводке дня — из профиля под предикатом происхождения (DRF-1844, F1).

До этого листа ``nutrition_summary_service`` ставил ``calories_goal = None``
безусловно («профиль подставлять нельзя до методики»). Методика §85
утверждена и реализована (DRF-2097, ``mifflin_st_jeor_v2``); теперь
ориентир уезжает в сводку **только** при действующем ориентире по виду —
``targets_state.calories_confirmed`` (``ayla_calculated`` /
``user_entered``), тем же предикатом, каким читается жидкость (F1(б)).

Узлы:

* s1 — ``ayla_calculated`` (подтверждённое предложение): число в ``calories_goal``,
  ключ в ответе ручки есть (красное до правки);
* s2 — ``user_entered`` (ручная норма): число (красное);
* s3 — ``ayla_proposed`` / ``none`` / ``unknown_legacy``: ключа нет, не 0
  (положительная тройка; сегодня зелёная, останется);
* s4 — подтверждённый источник, но ``daily_kcal`` пуст: ключа нет;
* s5 — положительная стража сторожа происхождения: предикат подменён на
  «всегда да» → число утекает при ``ayla_proposed`` (то есть именно предикат
  держит границу, а не совпадение данных);
* s6 — те же факты уходят в ``SummaryFacts`` для комментария (``calories_goal``).

Профиль в узлах рождается штатным путём (``upsert_profile`` → ``ayla_proposed``
→ ``confirm_targets`` → ``ayla_calculated``; ``set_manual_targets`` →
``user_entered``), не ``objects.create(daily_kcal=…)``: значение обязано
родиться там, где оно рождается на пилоте.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_summary_service import NutritionSummaryService

pytestmark = pytest.mark.django_db

SUMMARY_URL = "/api/v1/nutrition/summary/"
DAY = date(2026, 9, 18)
CONSENT = {"type": "personal_calculation", "document_version": "v1"}


@pytest.fixture
def user(db):
    from users.models import Profile, User

    u = User.objects.create_user(username="cg-1844", password="x", role="client", phone="+79993331844")
    Profile.objects.filter(user=u).update(full_name="Cg", city="Penza")
    return u


@pytest.fixture
def proposed_profile(user) -> NutritionProfile:
    """Анкета пройдена штатным путём — расчёт есть, но это ПРЕДЛОЖЕНИЕ."""
    from nutrition.services.profile_upsert_service import upsert_profile

    upsert_profile(
        user=user, external_user_id=str(user.id), idempotency_key=None,
        payload={
            "consent": CONSENT, "gender": "female", "age": 30, "height_cm": 170,
            "weight_kg": 70.0, "activity_coefficient": 1.375, "goal": "maintain",
            "pace": "moderate", "complete": True,
        },
    )
    profile = NutritionProfile.objects.get(user=user)
    assert profile.targets_source == NutritionProfile.TargetsSource.AYLA_PROPOSED
    assert profile.daily_kcal and profile.daily_kcal % 10 == 0  # v2: считано и округлено
    return profile


@pytest.fixture
def client(user) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=user)
    return c


def _summary_body(client: APIClient) -> dict:
    resp = client.get(SUMMARY_URL, {"date": DAY.isoformat()})
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


class TestTheGoalFollowsProvenance:
    def test_s1_a_confirmed_ayla_calculation_puts_the_goal_into_the_summary(self, client, user, proposed_profile):
        from nutrition.services.profile_upsert_service import confirm_targets

        _, outcome = confirm_targets(user=user, external_user_id=str(user.id))
        assert outcome == "confirmed"
        proposed_profile.refresh_from_db()
        assert proposed_profile.calories_source == NutritionProfile.TargetsSource.AYLA_CALCULATED

        body = _summary_body(client)

        assert body["calories_goal"] == proposed_profile.daily_kcal
        summary = NutritionSummaryService().summary(user_id=user.id, day=DAY)
        assert summary.calories_goal == proposed_profile.daily_kcal

    def test_s2_a_manual_norm_is_the_goal(self, client, user):
        from nutrition.services.manual_targets_service import set_manual_targets

        profile, report = set_manual_targets(user=user, calories_kcal=1800, water_ml=None)
        assert profile.calories_source == NutritionProfile.TargetsSource.USER_ENTERED

        assert _summary_body(client)["calories_goal"] == 1800

    def test_s3_a_proposal_none_and_legacy_send_no_key_not_zero(self, client, user, proposed_profile):
        assert "calories_goal" not in _summary_body(client)  # ayla_proposed — предложение, не ориентир

        for source in (NutritionProfile.TargetsSource.NONE, NutritionProfile.TargetsSource.UNKNOWN_LEGACY):
            NutritionProfile.objects.filter(user=user).update(
                targets_source=source, calories_source=source, fluids_source=source,
            )
            body = _summary_body(client)
            assert "calories_goal" not in body, (source, body.get("calories_goal"))
            assert body.get("calories_goal") != 0

    def test_s4_a_confirmed_source_without_a_number_sends_no_key(self, client, user, proposed_profile):
        NutritionProfile.objects.filter(user=user).update(
            targets_source=NutritionProfile.TargetsSource.AYLA_CALCULATED,
            calories_source=NutritionProfile.TargetsSource.AYLA_CALCULATED,
            daily_kcal=None,
        )
        assert "calories_goal" not in _summary_body(client)

    def test_s5_the_predicate_is_what_holds_the_line(self, client, user, proposed_profile):
        """Положительная стража сторожа происхождения: сними предикат — число утечёт."""
        assert "calories_goal" not in _summary_body(client)
        with patch(
            "nutrition.services.nutrition_summary_service.calories_confirmed", return_value=True
        ):
            leaked = _summary_body(client)
        assert leaked.get("calories_goal") == proposed_profile.daily_kcal  # то, что сторож обязан не пускать

    def test_s6_the_comment_facts_carry_the_same_goal(self, user, proposed_profile):
        from nutrition.services.profile_upsert_service import confirm_targets

        confirm_targets(user=user, external_user_id=str(user.id))
        seen: dict = {}

        class _Comment:
            def comment_for(self, *, user_id, day, facts):
                seen["goal"] = facts.calories_goal
                return "ok"

        with patch("nutrition.services.ai_comment_service.AICommentService", return_value=_Comment()):
            summary = NutritionSummaryService().summary(user_id=user.id, day=DAY, with_comment=True)
        proposed_profile.refresh_from_db()
        assert seen["goal"] == summary.calories_goal == proposed_profile.daily_kcal
