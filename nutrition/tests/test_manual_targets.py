"""Норма, заданная человеком сам — единственный писатель ``user_entered`` (§5.1).

Решение владельца 11.09.2026 §5.1: «Пользователь может задать норму
самостоятельно». До этого среза ``user_entered`` был значением в словаре
без писателя (grep по ``USER_ENTERED`` в services → только чтения).
Пороги — §85 (09.09.2026): ``< 1000`` отказ, ``1000–1199`` предупреждение,
``> 30 %`` от поддержания — подтверждение; вода вне ``1000–5000`` —
подтверждение.

Оговорка о предмете: ручки в боте пока нет — вызывающих ноль, стережём
механизм.
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.manual_targets_service import (
    CALORIES_HARD_FLOOR_KCAL,
    CALORIES_WARN_BELOW_KCAL,
    COMPUTED_FIELDS,
    WATER_MAX_ML,
    WATER_MIN_ML,
    maintenance_kcal,
)
from nutrition.services.nutrition_profile_service import ProfileInputs, compute_norms
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from nutrition.services.targets_state import targets_confirmed
from nutrition.services.water_entry_service import (
    CreateWaterInput,
    WaterEntryService,
    _load_nutrition_context,
)
from users.models import User

pytestmark = pytest.mark.django_db

URL_PROFILE = "/api/v1/nutrition/internal/profile/"
URL_MANUAL = "/api/v1/nutrition/internal/profile/targets/manual/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-manual"
Source = NutritionProfile.TargetsSource

FULL_INPUTS = {
    "gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0,
    "activity_coefficient": 1.4, "goal": "lose",
}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:manual", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:manual",
    }


def _manual(body, headers):
    return APIClient().post(URL_MANUAL, body, format="json", **headers)


def _compute(headers):
    resp = APIClient().post(URL_PROFILE, {**FULL_INPUTS, "consent": CONSENT}, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


# ===========================================================================
# Писатель: калории и вода — user_entered, посчитанное стёрто
# ===========================================================================


class TestWriter:
    def test_calories_alone_become_user_entered_and_act(self, proxy_user, headers):
        resp = _manual({"calories_kcal": 1800}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["norms"] == {"daily_kcal": 1800}
        assert body["targets_provenance"]["source"] == "user_entered"
        assert body["targets_provenance"]["confirmed_at"] is not None
        assert body["targets_provenance"]["input_snapshot"] == {}
        assert body["manual_targets"] == {
            "warnings": [], "deviation": {"deviation_check": "unavailable"}, "set": ["daily_kcal"],
        }

        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.USER_ENTERED
        assert p.daily_kcal == 1800 and p.daily_water_ml is None
        assert targets_confirmed(p) is True  # действует: тот же предикат, что у читателей

    def test_water_alone_reaches_the_water_context_and_responses(self, proxy_user, headers):
        resp = _manual({"water_ml": 2200}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["data"]["norms"] == {"daily_water_ml": 2200}

        ctx = _load_nutrition_context(proxy_user.id)
        assert ctx.fluid_target_ml == 2200
        entry = WaterEntryService().create(CreateWaterInput(
            user_id=proxy_user.id, ml=550, beverage_slug=None, ts=None, idempotency_key=None,
        ))
        assert entry.today_norm_water_ml == 2200
        assert entry.today_progress_pct == 25

    def test_manual_replaces_a_computed_set_and_clears_what_was_derived(self, proxy_user, headers):
        _compute(headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_protein_g is not None and p.bmr is not None  # POSITIVE: было посчитано

        resp = _manual({"calories_kcal": 1900, "water_ml": 2000, "confirm_deviation": True}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p.refresh_from_db()
        assert p.targets_source == Source.USER_ENTERED
        assert p.daily_kcal == 1900 and p.daily_water_ml == 2000
        for name in COMPUTED_FIELDS:
            assert getattr(p, name) is None, name
        assert p.targets_input_snapshot == {} and p.targets_method_versions == {}
        assert p.targets_computed_at is None and p.targets_confirmed_at is not None
        # Входы анкеты остаются — заменён расчёт, не данные человека.
        assert p.weight_kg == 67.0 and p.gender == "female"
        # Аудит дописан, не перезатёрт.
        assert p.last_overrides_applied[-1] == {
            "reason": "user_entered", "fields": ["daily_kcal", "daily_water_ml"], "warnings": [],
        }
        # Наружу — только названное человеком, без «белок: null».
        assert resp.json()["data"]["norms"] == {"daily_kcal": 1900, "daily_water_ml": 2000}

    def test_legacy_water_column_is_not_read_without_user_entered(self, proxy_user):
        """Остаток формулы 30 × вес в столбце — не ориентир ни при каком другом источнике."""
        for source in (Source.UNKNOWN_LEGACY, Source.AYLA_CALCULATED, Source.NONE):
            NutritionProfile.objects.update_or_create(
                user=proxy_user, defaults={"targets_source": source, "daily_water_ml": 2100},
            )
            assert _load_nutrition_context(proxy_user.id).fluid_target_ml is None, source
        NutritionProfile.objects.filter(user=proxy_user).update(targets_source=Source.USER_ENTERED)
        assert _load_nutrition_context(proxy_user.id).fluid_target_ml == 2100  # POSITIVE

    def test_nothing_to_set_is_a_400(self, proxy_user, headers):
        resp = _manual({}, headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert not NutritionProfile.objects.filter(user=proxy_user).exists()


# ===========================================================================
# Пороги §85
# ===========================================================================


class TestCalorieThresholds:
    def test_below_floor_is_refused_and_nothing_is_written(self, proxy_user, headers):
        resp = _manual({"calories_kcal": CALORIES_HARD_FLOOR_KCAL - 1, "water_ml": 2000}, headers)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.json()
        err = resp.json()["error"]
        assert err["code"] == "CALORIES_BELOW_FLOOR"
        assert err["details"] == {"calories_kcal": 999, "floor_kcal": 1000}
        # Отказ по калориям не оставляет за собой записанную воду.
        assert not NutritionProfile.objects.filter(user=proxy_user).exists()

    def test_floor_itself_is_accepted_with_a_warning(self, proxy_user, headers):
        resp = _manual({"calories_kcal": CALORIES_HARD_FLOOR_KCAL}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["data"]["manual_targets"]["warnings"] == ["calories_low"]
        assert NutritionProfile.objects.get(user=proxy_user).daily_kcal == 1000

    def test_warn_band_upper_edge_has_no_warning(self, proxy_user, headers):
        resp = _manual({"calories_kcal": CALORIES_WARN_BELOW_KCAL}, headers)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"]["manual_targets"]["warnings"] == []

    def test_deviation_over_30pct_needs_confirmation_and_names_maintenance(self, proxy_user, headers):
        _compute(headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        maintenance = maintenance_kcal(p)
        assert maintenance is not None and maintenance > 0  # POSITIVE: поддержание известно
        too_low = int(maintenance * 0.6)

        resp = _manual({"calories_kcal": too_low}, headers)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.json()
        err = resp.json()["error"]
        assert err["code"] == "CONFIRMATION_REQUIRED"
        assert err["details"]["kind"] == "calories_deviation"
        assert err["details"]["maintenance_kcal"] == maintenance
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_PROPOSED  # ничего не записано

        confirmed = _manual({"calories_kcal": too_low, "confirm_deviation": True}, headers)
        assert confirmed.status_code == status.HTTP_200_OK, confirmed.json()
        dev = confirmed.json()["data"]["manual_targets"]["deviation"]
        assert dev["deviation_check"] == "done" and dev["maintenance_kcal"] == maintenance
        assert dev["deviation_ratio"] > 0.3

    def test_within_30pct_passes_without_confirmation(self, proxy_user, headers):
        _compute(headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        maintenance = maintenance_kcal(p)
        resp = _manual({"calories_kcal": int(maintenance * 0.9)}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["data"]["manual_targets"]["deviation"]["deviation_check"] == "done"

    def test_maintenance_is_from_the_snapshot_not_from_profile_fields(self, proxy_user):
        """Поддержание считается от входов СОСТОЯВШЕГОСЯ расчёта; без снимка — не выдумывается."""
        p = NutritionProfile.objects.create(
            user=proxy_user, targets_source=Source.NONE, **FULL_INPUTS,
        )
        assert maintenance_kcal(p) is None  # входы есть, расчёта не было — не считаем
        p.targets_source = Source.AYLA_CALCULATED
        p.targets_input_snapshot = {
            "gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0,
            "activity_coefficient": 1.4, "goal": "lose", "pace": "moderate",
        }
        p.save()
        # Та же методика, что у расчёта, с целью maintain от снимка —
        # число сверяется с самим расчётом, а не с константой в тесте.
        expected = compute_norms(ProfileInputs(
            **{**p.targets_input_snapshot, "goal": "maintain", "pace": "moderate"},
        )).daily_kcal
        assert expected is not None and expected > 0
        assert maintenance_kcal(p) == expected


class TestWaterThresholds:
    @pytest.mark.parametrize("value", [WATER_MIN_ML - 1, WATER_MAX_ML + 1])
    def test_out_of_range_needs_reconfirmation(self, proxy_user, headers, value):
        resp = _manual({"water_ml": value}, headers)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.json()
        err = resp.json()["error"]
        assert err["details"]["kind"] == "water_out_of_range"
        assert not NutritionProfile.objects.filter(user=proxy_user).exists()

        confirmed = _manual({"water_ml": value, "confirm_water_out_of_range": True}, headers)
        assert confirmed.status_code == status.HTTP_200_OK, confirmed.json()
        assert confirmed.json()["data"]["manual_targets"]["warnings"] == ["water_out_of_range"]
        assert NutritionProfile.objects.get(user=proxy_user).daily_water_ml == value

    @pytest.mark.parametrize("value", [WATER_MIN_ML, WATER_MAX_ML])
    def test_range_edges_pass_silently(self, proxy_user, headers, value):
        resp = _manual({"water_ml": value}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        assert resp.json()["data"]["manual_targets"]["warnings"] == []


# ===========================================================================
# Взаимодействие с расчётом и подтверждением
# ===========================================================================


class TestInteractions:
    def test_recompute_with_attestation_replaces_manual_with_a_proposal(self, proxy_user, headers):
        _manual({"calories_kcal": 1800}, headers)
        _compute(headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal != 1800

    def test_partial_post_without_attestation_leaves_manual_intact(self, proxy_user, headers):
        _manual({"calories_kcal": 1800}, headers)
        resp = APIClient().post(URL_PROFILE, {"diet_preference": "vegetarian"}, format="json", **headers)
        assert resp.status_code == status.HTTP_200_OK
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.USER_ENTERED and p.daily_kcal == 1800

    def test_confirm_on_manual_is_nothing_to_confirm(self, proxy_user, headers):
        _manual({"calories_kcal": 1800}, headers)
        resp = APIClient().post(
            "/api/v1/nutrition/internal/profile/targets/confirm/", {}, format="json", **headers,
        )
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["error"]["details"]["targets_source"] == "user_entered"
