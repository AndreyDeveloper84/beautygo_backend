"""Темп и активность — только названные человеком (CD §72, вопрос 59).

Решение владельца: темп и активность не подставлять; спрашивать в анкете;
без ответа — «не хватает данных» с именем поля, как пол и цель в #527
(DRF-2219). До правки:

* ``profile_upsert_service`` считал с ``pace or "moderate"`` и
  ``activity_coefficient or DEFAULT_ACTIVITY`` (1.4 — вне утверждённого
  набора, дальше подводилось к 1.375);
* строка профиля рождалась с ``activity_coefficient=1.4`` (умолчание схемы
  и ``get_or_create(defaults=…)``) — человек ещё ничего не назвал, а
  «активность» уже была.

Темп влияет на число только при цели, отличной от «поддержания»
(``_kcal_from_goal``: при ``maintain`` поправка нулевая), поэтому и
обязателен он только там — спрашивать темп у того, кто держит вес, значит
спрашивать то, чего расчёт не использует.

Узлы идут через ``upsert_profile`` (эндпоинт), как у #527.

* q1 — «похудеть» без темпа: расчёта нет, отказ называет ``pace``;
* q2 — «поддерживать» без темпа: расчёт есть (темп не нужен), в снимке его
  нет — выдуманного «moderate» там не появляется;
* q3 — без активности: расчёта нет, отказ называет ``activity_coefficient``;
* q4 — новая строка профиля не рождается с выдуманной активностью;
* q5 — всё названо: расчёт есть (присутствие).

Узлы ревью (upsert частичный — поле, которого нет в теле, остаётся):

* r1 — повторная анкета с «Не знаю» про активность (бот шлёт пропуск, не
  число): прежняя активность снимается, расчёта нет; названная затем —
  пометка пропуска снимается, расчёт есть;
* r2 — ступень пола BMR (moderate → gentle) не переписывает названный темп
  в профиле — расчётный темп в снимке;
* r3 — смена цели на «поддерживать»: прежний темп в снимок не попадает.
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-q59"
Source = NutritionProfile.TargetsSource

BODY = {"gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:q59", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:q59",
    }


def _post(body, headers):
    resp = APIClient().post(URL, {**body, "consent": CONSENT}, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _missing(profile: NutritionProfile) -> list[str]:
    for entry in profile.last_overrides_applied or []:
        if isinstance(entry, dict) and entry.get("reason") == "insufficient_inputs":
            return list(entry.get("fields") or [])
    return []


class TestPace:
    def test_lose_without_pace_is_not_calculated(self, proxy_user, headers):
        _post({**BODY, "goal": "lose", "activity_coefficient": 1.375}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None
        assert "pace" in _missing(p)

    def test_maintain_without_pace_is_calculated_and_invents_no_pace(self, proxy_user, headers):
        _post({**BODY, "goal": "maintain", "activity_coefficient": 1.375}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal and p.daily_kcal > 0
        assert p.targets_input_snapshot.get("pace") in (None, "")
        assert p.pace == ""


class TestActivity:
    def test_no_activity_is_not_calculated(self, proxy_user, headers):
        _post({**BODY, "goal": "maintain"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None
        assert "activity_coefficient" in _missing(p)

    def test_a_new_profile_is_not_born_with_an_activity(self, proxy_user, headers):
        _post({"diet_preference": "vegetarian"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        # Присутствие: строка создана — отсутствие ниже про активность.
        assert p.diet_preference == "vegetarian"
        assert p.activity_coefficient is None


class TestEverythingNamed:
    def test_all_inputs_named_still_get_a_number(self, proxy_user, headers):
        _post(
            {**BODY, "goal": "lose", "pace": "gentle", "activity_coefficient": 1.55},
            headers,
        )
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal and p.daily_kcal > 0
        assert p.targets_input_snapshot["pace"] == "gentle"
        assert p.targets_input_snapshot["activity_coefficient"] == 1.55


class TestPartialUpsertDoesNotKeepWhatWasNotNamed:
    def test_unknown_activity_on_a_retake_clears_the_old_one(self, proxy_user, headers):
        _post({**BODY, "goal": "maintain", "activity_coefficient": 1.725}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        # Присутствие: первый проход посчитан от названной активности.
        assert p.targets_source == Source.AYLA_PROPOSED

        _post({**BODY, "goal": "maintain", "_skipped_fields": ["activity"]}, headers)
        p.refresh_from_db()
        assert p.activity_coefficient is None
        assert p.targets_source == Source.NONE
        assert "activity_coefficient" in _missing(p)
        assert p.health_flags.get("activity_skipped") is True

        _post({**BODY, "goal": "maintain", "activity_coefficient": 1.2}, headers)
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_PROPOSED
        assert "activity_skipped" not in p.health_flags

    def test_the_pace_step_does_not_overwrite_the_named_pace(self, proxy_user, headers):
        # Только шаг темпа (moderate → gentle), цель остаётся «похудеть».
        _post(
            {
                "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 40.0,
                "activity_coefficient": 1.2, "goal": "lose", "pace": "moderate",
            },
            headers,
        )
        p = NutritionProfile.objects.get(user=proxy_user)
        assert {
            "reason": "bmr_floor",
            "from": {"pace": "moderate"},
            "to": {"pace": "gentle"},
        } in p.last_overrides_applied  # присутствие: шаг темпа сработал
        assert p.targets_input_snapshot["goal"] == "lose"
        assert p.targets_input_snapshot["pace"] == "gentle"
        assert p.pace == "moderate"

    def test_a_goal_flip_to_maintain_carries_no_pace(self, proxy_user, headers):
        # Оба шага: lose → gentle → maintain.
        _post(
            {
                "gender": "female", "age": 30, "height_cm": 160, "weight_kg": 40.0,
                "activity_coefficient": 1.2, "goal": "lose", "pace": "moderate",
            },
            headers,
        )
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_input_snapshot["goal"] == "maintain"  # присутствие
        assert p.targets_input_snapshot.get("pace") in (None, "")
        assert p.goal == "lose" and p.pace == "moderate"

    def test_a_goal_without_pace_does_not_reuse_an_old_pace(self, proxy_user, headers):
        _post({**BODY, "goal": "lose", "pace": "gentle", "activity_coefficient": 1.55}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.pace == "gentle"  # присутствие

        _post({**BODY, "goal": "gain", "activity_coefficient": 1.55}, headers)
        p.refresh_from_db()
        assert p.pace == ""
        assert "pace" in _missing(p)

    def test_maintain_does_not_carry_an_old_pace_into_the_snapshot(self, proxy_user, headers):
        _post({**BODY, "goal": "lose", "pace": "gentle", "activity_coefficient": 1.55}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_input_snapshot["pace"] == "gentle"  # присутствие

        _post({**BODY, "goal": "maintain", "activity_coefficient": 1.55}, headers)
        p.refresh_from_db()
        assert p.targets_input_snapshot["goal"] == "maintain"
        assert p.targets_input_snapshot.get("pace") in (None, "")
