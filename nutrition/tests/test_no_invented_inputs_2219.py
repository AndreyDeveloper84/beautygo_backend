"""Расчёт не идёт от выдуманных пола и цели (DRF-2219, §63).

Требование владельца §63: «никакого расчёта от выдуманных параметров; нет
входа — расчёта нет, честное "не хватает данных"». ``upsert_profile``
подставлял ``gender or "female"`` и ``goal or "maintain"``: человек, не
назвавший пол, получал ориентир по женской формуле Миффлина — Сан Жеора,
не назвавший цель — ориентир «поддержание», и оба выглядели посчитанными
от его данных.

Узлы идут через ``upsert_profile`` (эндпоинт), а не через чистую
``compute_norms``: сторож ``test_targets_absent`` держит функцию, а
подстановка жила в вызывающем, и его он не видел (уточнение (г)).

Темп и активность — вне объёма: их подстановки ждут решения владельца
(вопрос 59) — бот ``pace`` не шлёт вовсе и сознательно шлёт умолчание
активности при пропуске вопроса.

* g1 — без пола: расчёта нет, отказ назван ``insufficient_inputs`` с полем
  ``gender``; ориентира нет;
* g2 — без цели: то же с полем ``goal``;
* g3 — отказ не записывает в профиль выдуманную цель «maintain»;
* g4 — пол и цель названы: расчёт есть (присутствие — сторож не отказывает
  всем подряд).
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
SERVICE_TOKEN = "svc-token-2219"
Source = NutritionProfile.TargetsSource

BODY = {"age": 36, "height_cm": 170, "weight_kg": 67.0, "activity_coefficient": 1.375}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:2219", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:2219",
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


class TestNoInventedGenderOrGoal:
    def test_no_gender_means_no_calculation(self, proxy_user, headers):
        body = _post({**BODY, "goal": "maintain"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None and p.bmr is None
        assert "gender" in _missing(p)
        assert body["targets_provenance"]["source"] == "none"

    def test_no_goal_means_no_calculation(self, proxy_user, headers):
        _post({**BODY, "gender": "male"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None
        assert "goal" in _missing(p)

    def test_a_refusal_does_not_write_an_invented_goal(self, proxy_user, headers):
        _post({**BODY, "gender": "male"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        # Присутствие: отказ состоялся и назван.
        assert "goal" in _missing(p)
        assert p.goal == ""

    def test_named_gender_and_goal_still_get_a_number(self, proxy_user, headers):
        _post({**BODY, "gender": "male", "goal": "maintain"}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal and p.daily_kcal > 0
        assert p.targets_input_snapshot["gender"] == "male"
        assert p.targets_input_snapshot["goal"] == "maintain"
