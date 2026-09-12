"""N-g: при health-факторах Ayla НЕ рассчитывает норму — отказ с именем (§5.1).

Решение владельца 11.09.2026 §5.1: «При health-факторах Ayla не
рассчитывает индивидуальную норму». Состав факторов — решение главного
окна 11.09 по §5.1/§85: ``pregnant``, ``breastfeeding``,
``eating_disorder`` (флаги профиля) и несовершеннолетний возраст.

Прежде лестница поправок СЧИТАЛА при этих флагах (maintain, +200/+400
ккал, +25 г белка) — число там, где владелец запретил число. Теперь:
норма не считается, предложение (``ayla_proposed``) не создаётся,
``targets_source = none``, в аудите ``health_factor_<имя>`` на КАЖДЫЙ
фактор. Стража положительная на каждый фактор отдельно и парная: тот же
профиль без фактора — считает.

Оговорка о предмете: анкета на пилоте закрыта fail-closed (#1523), живых
запросов с этими флагами сегодня нет — стережём механизм.
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.nutrition_profile_service import (
    ADULT_AGE,
    HEALTH_FACTOR_FLAGS,
    ProfileInputs,
    compute_norms,
)
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-ng"
Source = NutritionProfile.TargetsSource

ADULT = dict(
    gender="female", age=30, height_cm=165, weight_kg=65.0,
    activity_coefficient=1.4, goal="lose", pace="moderate",
)


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:ng", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:ng",
    }


def _post(body, headers):
    return APIClient().post(URL, body, format="json", **headers)


# ===========================================================================
# Чистая функция: каждый фактор — отказ по имени; без фактора — расчёт
# ===========================================================================


class TestEachFactorRefusesByName:
    def test_the_set_is_exactly_four(self):
        """Состав — решение, не догадка: три флага + несовершеннолетие."""
        assert HEALTH_FACTOR_FLAGS == ("pregnant", "breastfeeding", "eating_disorder")
        assert ADULT_AGE == 18

    @pytest.mark.parametrize("flag", HEALTH_FACTOR_FLAGS)
    def test_flag_refuses_and_its_pair_without_the_flag_computes(self, flag):
        refused = compute_norms(ProfileInputs(**ADULT, health_flags={flag: True}))
        assert refused.computed is False
        assert refused.bmr is None and refused.daily_kcal is None
        assert refused.daily_protein_g is None and refused.daily_iron_mg is None
        assert refused.input_snapshot == {} and refused.method_versions == {}
        assert refused.goal_overridden_by == ""
        assert refused.goal == "lose"  # цель не переписана в maintain
        assert [o["reason"] for o in refused.overrides_applied] == [f"health_factor_{flag}"]

        # POSITIVE (пара): тот же человек без фактора — считает.
        computed = compute_norms(ProfileInputs(**ADULT, health_flags={flag: False}))
        assert computed.computed is True
        assert computed.daily_kcal is not None and computed.daily_kcal > 0

    def test_minor_age_refuses_by_name_and_adult_age_computes(self):
        minor = compute_norms(ProfileInputs(**{**ADULT, "age": ADULT_AGE - 1}))
        assert minor.computed is False
        assert minor.daily_kcal is None
        assert [o["reason"] for o in minor.overrides_applied] == ["health_factor_minor"]

        adult = compute_norms(ProfileInputs(**{**ADULT, "age": ADULT_AGE}))
        assert adult.computed is True

    def test_several_factors_are_all_named_in_a_stable_order(self):
        norms = compute_norms(ProfileInputs(
            **{**ADULT, "age": 16},
            health_flags={"eating_disorder": True, "pregnant": True},
        ))
        assert [o["reason"] for o in norms.overrides_applied] == [
            "health_factor_pregnant", "health_factor_eating_disorder", "health_factor_minor",
        ]

    def test_health_factor_wins_over_missing_inputs(self):
        """Safety раньше полноты входов (§4 владельца: приоритет правил)."""
        norms = compute_norms(ProfileInputs(
            **{**ADULT, "weight_kg": None}, health_flags={"pregnant": True},
        ))
        assert [o["reason"] for o in norms.overrides_applied] == ["health_factor_pregnant"]

    def test_unknown_age_is_missing_input_not_a_health_factor(self):
        norms = compute_norms(ProfileInputs(**{**ADULT, "age": None}))
        assert [o["reason"] for o in norms.overrides_applied] == ["insufficient_inputs"]

    def test_the_old_ladder_is_gone(self):
        """Поправок больше нет — ни констант, ни имён в аудите."""
        import nutrition.services.nutrition_profile_service as mod

        for name in ("PREGNANCY_KCAL_BONUS", "BREASTFEEDING_KCAL_BONUS", "PREGNANCY_PROTEIN_BONUS_G"):
            assert not hasattr(mod, name), name
        norms = compute_norms(ProfileInputs(**ADULT, health_flags={"pregnant": True}))
        reasons = {o["reason"] for o in norms.overrides_applied}
        assert "pregnancy" not in reasons and "eating_disorder" not in reasons


# ===========================================================================
# Через ручку: отказ доезжает как отсутствие + имя, предложения нет
# ===========================================================================


class TestRefusalTravelsThroughTheEndpoint:
    @pytest.mark.parametrize("flag", HEALTH_FACTOR_FLAGS)
    def test_flag_gives_no_proposal_and_a_named_reason(self, proxy_user, headers, flag):
        resp = _post({**ADULT, "consent": CONSENT, "health_flags": {flag: True}}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["norms"] == {}
        assert body["targets_provenance"]["source"] == "none"
        assert body["targets_provenance"]["input_snapshot"] == {}
        assert [o["reason"] for o in body["overrides_applied"]] == [f"health_factor_{flag}"]

        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None
        assert p.health_flags.get(flag) is True  # флаг сохранён — отказ его не стирает

    def test_flag_added_later_turns_a_proposal_into_a_named_refusal(
        self, proxy_user, headers,
    ):
        """Было предложение; человек назвал беременность — числа сняты, имя есть."""
        _post({**ADULT, "consent": CONSENT}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_PROPOSED

        resp = _post({"health_flags": {"pregnant": True}}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p.refresh_from_db()
        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None and p.targets_input_snapshot == {}
        assert [o["reason"] for o in p.last_overrides_applied] == ["health_factor_pregnant"]

    def test_minor_through_the_endpoint(self, proxy_user, headers):
        resp = _post({**ADULT, "age": 16, "consent": CONSENT}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["norms"] == {}
        assert [o["reason"] for o in body["overrides_applied"]] == ["health_factor_minor"]
