"""DRF-2241 — остатки выдуманных входов расчёта после #527 (DRF-2219).

Замер на ``origin/dev`` 33eddecc. Темп и активность — вне объёма (вопрос 59).

* (1) снимок ``targets_input_snapshot`` строки, посчитанной ДО #527, несёт
  выдуманный ``gender="female"`` (тогда ``upsert`` подставлял его сам).
  Снимок читает ``manual_targets_service.maintenance_kcal`` — проверка
  отклонения ручной нормы считается по женской формуле для человека, пол не
  называвшего, — и он уходит наружу в ``targets_provenance.input_snapshot``
  как «использованные данные» (§5.1);
* (2) ступень ``bmr_floor`` (lose → maintain) пишет результат обратно в
  ВХОД: ``_recompute_and_persist`` делает ``profile.goal = norms.goal``.
  Названная цель «похудеть» стирается из профиля, и следующий пересчёт — уже
  при другом весе, где ступень не нужна, — идёт от «поддержания»;
* (3) ``compute_norms`` проверяет НАЛИЧИЕ пола и цели, не ДОПУСТИМОСТЬ:
  ``gender="x"`` считается по женской формуле (``else -161``), ``goal="x"``
  — молча как «поддержание» (``GOAL_FACTORS.get(goal, 1.0)``). Эндпоинт
  значения проверяет (``ChoiceField``), функция — нет, а в неё приходят и
  снимки (``maintenance_kcal``), и строки, записанные мимо сериализатора.
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.manual_targets_service import maintenance_kcal
from nutrition.services.nutrition_profile_service import ProfileInputs, compute_norms
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-2241"
Source = NutritionProfile.TargetsSource

SNAPSHOT = {
    "gender": "female",
    "age": 36,
    "height_cm": 170,
    "weight_kg": 67.0,
    "activity_coefficient": 1.375,
}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def user(db):
    return User.objects.create(username="bot:2241", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {"HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN, "HTTP_X_EXTERNAL_USER_ID": "bot:2241"}


def _post(body, headers):
    resp = APIClient().post(URL, {**body, "consent": CONSENT}, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _legacy_row(user, *, gender: str) -> NutritionProfile:
    """Строка, посчитанная до #527: снимок с «female», подпись расчёта."""
    return NutritionProfile.objects.create(
        user=user,
        gender=gender,
        age=36,
        height_cm=170,
        weight_kg=67.0,
        goal="maintain",
        daily_kcal=2000,
        targets_source=Source.AYLA_CALCULATED,
        calories_source=Source.AYLA_CALCULATED,
        targets_input_snapshot=dict(SNAPSHOT),
        targets_method_versions={"calories": "mifflin_st_jeor_v2"},
    )


# ── (1) старый «female» в снимке ─────────────────────────────────────


class TestP1LegacySnapshotGender:
    def test_maintenance_is_not_computed_from_an_unnamed_gender(self, user) -> None:
        profile = _legacy_row(user, gender="")
        assert profile.targets_input_snapshot["gender"] == "female"  # положительно: снимок такой
        assert maintenance_kcal(profile) is None

    def test_the_unnamed_gender_is_not_shown_as_used_data(self, user, headers) -> None:
        _legacy_row(user, gender="")
        resp = APIClient().get(URL, **headers)
        assert resp.status_code == status.HTTP_200_OK
        snapshot = resp.json()["data"]["targets_provenance"]["input_snapshot"]
        assert snapshot  # положительно: снимок отдаётся
        assert snapshot.get("gender") != "female"

    def test_a_named_gender_keeps_the_maintenance_check(self, user) -> None:
        """Контроль: пол назван и совпадает со снимком — проверка работает."""
        assert maintenance_kcal(_legacy_row(user, gender="female")) is not None


# ── (2) ступень bmr_floor не стирает названную цель ──────────────────


LOW_WEIGHT = {
    "gender": "female",
    "age": 30,
    "height_cm": 160,
    "weight_kg": 40.0,
    "activity_coefficient": 1.2,
    "goal": "lose",
    # Вопрос 59: темп назван — лестница пола BMR (moderate → gentle →
    # maintain) проверяется от названного, а не от умолчания.
    "pace": "moderate",
}


class TestP2FloorDoesNotOverwriteTheNamedGoal:
    def test_the_named_goal_stays_in_the_profile(self, user, headers) -> None:
        data = _post(LOW_WEIGHT, headers)
        assert data["goal_overridden_by"] == "bmr_floor"  # положительно: ступень сработала
        assert NutritionProfile.objects.get(user=user).goal == "lose"

    def test_the_next_recompute_starts_from_the_named_goal(self, user, headers) -> None:
        _post(LOW_WEIGHT, headers)
        data = _post({"weight_kg": 70.0}, headers)
        assert data["goal_overridden_by"] in (None, "")  # положительно: ступень не нужна
        assert data["goal"] == "lose"


# ── (3) закрытый список значений в compute_norms ─────────────────────


def _inputs(**over) -> ProfileInputs:
    base = dict(
        gender="female", age=36, height_cm=170, weight_kg=67.0,
        activity_coefficient=1.375, goal="maintain", pace="moderate", health_flags={},
    )
    base.update(over)
    return ProfileInputs(**base)


class TestP3ClosedValueLists:
    @pytest.mark.parametrize("gender", ["x", "f", "женский", "FEMALE"])
    def test_an_unknown_gender_is_a_refusal(self, gender) -> None:
        norms = compute_norms(_inputs(gender=gender))
        assert norms.computed is False
        assert norms.daily_kcal is None

    @pytest.mark.parametrize("goal", ["x", "похудеть", "LOSE"])
    def test_an_unknown_goal_is_a_refusal(self, goal) -> None:
        norms = compute_norms(_inputs(goal=goal))
        assert norms.computed is False
        assert norms.daily_kcal is None

    @pytest.mark.parametrize("gender", ["female", "male"])
    @pytest.mark.parametrize("goal", ["lose", "maintain", "gain", "tone"])
    def test_every_allowed_pair_is_computed(self, gender, goal) -> None:
        """Контроль: все допустимые значения считаются."""
        assert compute_norms(_inputs(gender=gender, goal=goal)).computed is True
