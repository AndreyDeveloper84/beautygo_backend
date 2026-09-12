"""Integration + unit tests for /api/v1/nutrition/internal/profile/ (DRF-300).

Two layers:
- ``compute_norms`` pure-math: Mifflin-St Jeor reference, override
  ladder priorities (eating_disorder > pregnancy > BMR floor),
  defaults for skipped fields.
- HTTP layer: GET no-profile envelope, POST upsert + idempotency-key
  cache, validation ranges, complete=true onboarded_at flip,
  health_flags merge semantics.

Spec: docs/plans/maxbot-phase3-ayla-spec.md §1.
Acceptance: docs/plans/maxbot-phase3-linear-issues.md DRF-300.
"""
from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile, ProfileIdempotencyKey
from nutrition.services.nutrition_profile_service import (
    ProfileInputs,
    compute_norms,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-300"
URL = "/api/v1/nutrition/internal/profile/"

#: §92 / срез N-a2: параметры тела принимаются только с утверждением о
#: согласии. Предмет этих тестов — расчёт, идемпотентность и
#: PATCH-семантика; утверждение здесь часть ВАЛИДНОГО запроса, а не
#: предмет проверки. Сам сторож проверяется в
#: ``test_personal_calculation_consent.py``.
CONSENT = {"type": "personal_calculation", "document_version": "v1"}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(
        username="bot:300", role="client", is_proxy=True,
    )


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:300",
    }


# ===========================================================================
# Pure-math layer
# ===========================================================================


class TestMifflinStJeor:
    def test_female_165cm_70kg_40y_matches_reference(self):
        norms = compute_norms(ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=70.0,
            goal="maintain", pace="moderate",
        ))
        # Mifflin: 10×70 + 6.25×165 - 5×40 - 161 = 700 + 1031.25 - 200 - 161 = 1370.25
        assert norms.bmr == 1370

    def test_male_180cm_80kg_30y_matches_reference(self):
        norms = compute_norms(ProfileInputs(
            gender="male", age=30, height_cm=180, weight_kg=80.0,
            goal="maintain", pace="moderate",
        ))
        # 10×80 + 6.25×180 - 5×30 + 5 = 800 + 1125 - 150 + 5 = 1780
        assert norms.bmr == 1780


class TestSkippedFieldsCancelTheCalculation:
    """Тест назывался ``TestDefaultsForSkippedFields`` и проверял ДЕФЕКТ.

    Он утверждал: человек, не заполнивший ничего, получает те же нормы,
    что и «female / 40 / 165 / 70» — медиана пензенской аудитории. То
    есть закреплял, что ориентир считается ОТ ЧУЖОГО ТЕЛА, а на экране
    это неотличимо от своего.

    Владелец снял подстановку 09.09.2026 (§82, §85; раздел 3.2 решения
    перечисляет возраст, рост, вес и пол как обязательные входы).
    Утверждение перевёрнуто: расчёта нет, и у отказа есть имя.

    Поимённая проверка каждого из четырёх полей по одному —
    ``nutrition/tests/test_targets_absent.py::TestNobodyGetsSomeoneElsesBody``.
    """

    def test_all_fields_missing_yields_no_norms_at_all(self):
        norms = compute_norms(ProfileInputs(goal="maintain", pace="moderate"))
        # ``None``, не ноль (§103): отказ — отсутствие, а не число.
        assert norms.bmr is None
        assert norms.daily_kcal is None
        assert [o.get("reason") for o in norms.overrides_applied] == [
            "insufficient_inputs",
        ]
        assert set(norms.overrides_applied[0]["fields"]) == {
            "gender", "age", "height_cm", "weight_kg",
        }

    def test_a_complete_anketa_is_untouched(self):
        """Контроль присутствия: отказ адресный, а не поголовный."""
        norms = compute_norms(ProfileInputs(
            gender="female", age=40, height_cm=165, weight_kg=70.0,
            goal="maintain", pace="moderate",
        ))
        assert norms.bmr > 0
        assert norms.daily_kcal > 0


# ===========================================================================
# Override ladder
# ===========================================================================


class TestHealthFactorsRefuse:
    """§5.1 (11.09.2026): при health-факторах Ayla НЕ рассчитывает норму.

    Прежде здесь стояли `TestEatingDisorderOverride` и
    `TestPregnancyOverride` — лестница поправок (maintain, +200/+400 ккал,
    +25 г белка). Она считала число там, где владелец запретил считать.
    Теперь — отказ с именем на каждый фактор; числа нет ни одного.
    """

    @pytest.mark.parametrize("flag", ["eating_disorder", "pregnant", "breastfeeding"])
    def test_each_flag_refuses_by_name(self, flag):
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=65.0,
            goal="lose", pace="moderate",
            health_flags={flag: True},
        ))
        assert norms.computed is False
        assert norms.daily_kcal is None and norms.bmr is None
        assert norms.daily_iron_mg is None  # RDA тоже не считается
        assert norms.goal_overridden_by == ""
        assert [o["reason"] for o in norms.overrides_applied] == [f"health_factor_{flag}"]

    def test_two_flags_are_two_names_not_one_winner(self):
        """Раньше РПП «побеждал» беременность; теперь оба названы."""
        norms = compute_norms(ProfileInputs(
            gender="female", age=30, height_cm=165, weight_kg=65.0,
            goal="lose", pace="moderate",
            health_flags={"eating_disorder": True, "pregnant": True},
        ))
        assert norms.daily_kcal is None
        assert [o["reason"] for o in norms.overrides_applied] == [
            "health_factor_pregnant", "health_factor_eating_disorder",
        ]


class TestBmrFloorLadder:
    def test_pace_softens_first(self):
        # Pick a setup where lose+moderate falls under BMR floor but
        # lose+gentle stays above it.
        norms = compute_norms(ProfileInputs(
            gender="female", age=50, height_cm=160, weight_kg=55.0,
            activity_coefficient=1.2,
            goal="lose", pace="moderate",
        ))
        # If the moderate→gentle softening was enough we expect pace
        # changed but goal stays "lose".
        if norms.goal == "lose":
            assert norms.pace == "gentle"
            assert any(
                o["reason"] == "bmr_floor" and o["to"].get("pace") == "gentle"
                for o in norms.overrides_applied
            )

    def test_falls_through_to_maintain_when_gentle_still_undercuts(self):
        # Very low activity user where even gentle deficit drops below
        # BMR + margin.
        norms = compute_norms(ProfileInputs(
            gender="female", age=70, height_cm=150, weight_kg=45.0,
            activity_coefficient=1.0,
            goal="lose", pace="moderate",
        ))
        # Either pace softened to gentle was enough OR we landed on maintain.
        if norms.goal == "maintain":
            assert any(
                o["reason"] == "bmr_floor" and o["to"].get("goal") == "maintain"
                for o in norms.overrides_applied
            )


# ===========================================================================
# HTTP layer
# ===========================================================================


class TestGetProfile:
    def test_no_profile_returns_exists_false(self, proxy_user, headers):
        c = APIClient()
        resp = c.get(URL, **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["exists"] is False
        assert body["external_user_id"] == "bot:300"

    def test_existing_profile_returns_full_envelope(self, proxy_user, headers):
        # Seed via POST first
        c = APIClient()
        c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        resp = c.get(URL, **headers)
        body = resp.json()["data"]
        assert body["exists"] is True
        assert body["norms"]["bmr"] > 0
        assert body["norms"]["daily_kcal"] > 0


class TestPostProfileValidation:
    def test_age_below_min_rejected(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {"consent": CONSENT, "age": 14}, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_age_above_max_rejected(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {"consent": CONSENT, "age": 105}, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_weight_out_of_range_rejected(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {"consent": CONSENT, "weight_kg": 25}, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_height_out_of_range_rejected(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {"consent": CONSENT, "height_cm": 100}, format="json", **headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_health_flag_rejected(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(
            URL,
            {"health_flags": {"made_up_flag": True}},
            format="json",
            **headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


class TestPostProfileUpsert:
    def test_first_post_creates_profile_with_norms(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "activity_coefficient": 1.4,
            "goal": "lose", "pace": "moderate",
        }, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["exists"] is True
        assert body["norms"]["bmr"] == 1370
        assert NutritionProfile.objects.filter(user=proxy_user).exists()

    def test_patch_semantics_only_provided_fields_change(self, proxy_user, headers):
        c = APIClient()
        c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain", "pace": "moderate",
        }, format="json", **headers)
        # Send only weight_kg — age/height/gender/goal must persist.
        resp = c.post(URL, {"consent": CONSENT, "weight_kg": 72.0}, format="json", **headers)
        body = resp.json()["data"]
        assert body["weight_kg"] == 72.0
        assert body["age"] == 40
        assert body["height_cm"] == 165

    def test_skipped_fields_set_skipped_flags(self, proxy_user, headers):
        c = APIClient()
        resp = c.post(URL, {
            "consent": CONSENT,
            "gender": "female",
            "_skipped_fields": ["weight", "age"],
        }, format="json", **headers)
        flags = resp.json()["data"]["health_flags"]
        assert flags.get("weight_skipped") is True
        assert flags.get("age_skipped") is True

    def test_complete_true_sets_onboarded_at(self, proxy_user, headers):
        c = APIClient()
        r = c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "maintain", "pace": "moderate", "complete": True,
        }, format="json", **headers)
        assert r.json()["data"]["onboarded_at"] is not None

    def test_complete_does_not_overwrite_existing_onboarded_at(
        self, proxy_user, headers,
    ):
        c = APIClient()
        r1 = c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "complete": True,
        }, format="json", **headers)
        first_ts = r1.json()["data"]["onboarded_at"]
        r2 = c.post(URL, {"complete": True}, format="json", **headers)
        assert r2.json()["data"]["onboarded_at"] == first_ts

    def test_pregnant_returns_a_named_refusal_not_a_number(self, proxy_user, headers):
        """§5.1: health-фактор — отказ по имени, нормы нет, цель не тронута."""
        c = APIClient()
        resp = c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 65.0,
            "goal": "lose", "pace": "moderate",
            "health_flags": {"pregnant": True},
        }, format="json", **headers)
        body = resp.json()["data"]
        assert body["goal"] == "lose"  # лестница не переписала цель
        assert body["goal_overridden_by"] is None
        assert body["norms"] == {}
        assert body["targets_provenance"]["source"] == "none"
        reasons = [o["reason"] for o in body["overrides_applied"]]
        assert reasons == ["health_factor_pregnant"]


class TestIdempotencyKey:
    def test_replay_returns_cached_response(self, proxy_user, headers):
        c = APIClient()
        body = {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
            "goal": "lose", "pace": "moderate",
        }
        h = {**headers, "HTTP_IDEMPOTENCY_KEY": "abc-300"}
        r1 = c.post(URL, body, format="json", **h)
        r2 = c.post(URL, body, format="json", **h)
        assert r1.json() == r2.json()
        assert ProfileIdempotencyKey.objects.filter(key="abc-300").exists()

    def test_replay_does_not_double_apply_patch(self, proxy_user, headers):
        c = APIClient()
        h = {**headers, "HTTP_IDEMPOTENCY_KEY": "abc-300-2"}
        # 1st call sets weight 70 with the idem key
        c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
        }, format="json", **h)
        # Mutate without idem key to weight 80
        c.post(URL, {"consent": CONSENT, "weight_kg": 80.0}, format="json", **headers)
        # Replay 1st call — must NOT revert to 70 in DB; cached response is returned.
        r3 = c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165, "weight_kg": 70.0,
        }, format="json", **h)
        assert r3.json()["data"]["weight_kg"] == 70.0  # cached response
        # But DB still has 80 from the in-between non-idempotent write
        profile = NutritionProfile.objects.get(user=proxy_user)
        assert profile.weight_kg == 80.0


# ===========================================================================
# Integration with WaterEntryService (DRF-302) — profile-aware context
# ===========================================================================


class TestProfileWiresIntoWaterContext:
    def test_eating_disorder_profile_strips_water_response_fields(
        self, proxy_user, headers,
    ):
        from django.core.management import call_command
        call_command("seed_beverages")

        # Create profile with eating_disorder=True
        c = APIClient()
        c.post(URL, {
            "consent": CONSENT,
            "gender": "female", "age": 30, "height_cm": 165, "weight_kg": 60.0,
            "goal": "lose", "pace": "moderate",
            "health_flags": {"eating_disorder": True},
        }, format="json", **headers)

        # POST water — eating_disorder mode must strip kcal/milestone/alcohol
        water_resp = c.post(
            "/api/v1/nutrition/internal/water/",
            {"ml": 1500, "beverage_slug": "vino_krasnoe"},
            format="json",
            **headers,
        )
        body = water_resp.json()["data"]
        assert body["kcal"] == 0
        assert body["milestone_text"] is None
        assert body["alcohol_recovery_hint"] is False
