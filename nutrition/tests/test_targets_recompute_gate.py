"""Пересчёт ориентиров — только с основанием (§103, срез N-b).

Три окна между «ориентиры без происхождения» и «ориентиры после
согласия»:

* отказ расчёта писал нули — теперь ``NULL`` (часть 2 брифа);
* между очисткой (§103) и удалением входов (§120) любой POST без
  утверждения воскресил бы ориентиры от входов без основания и подписал
  бы их ``ayla_calculated`` — закрыто сторожем
  ``targets_recompute_gate.recompute_permitted`` (часть 4);
* положительная сторона того же сторожа: с утверждением считает, у
  ``ayla_calculated`` считает без утверждения (сценарий б).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services import profile_upsert_service
from nutrition.services.personal_calculation_consent import (
    PERSONAL_CALCULATION,
)
from nutrition.services.targets_recompute_gate import (
    RECOMPUTE_REFUSED_NO_CONSENT,
    recompute_permitted,
)
from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-nb"
URL = "/api/v1/nutrition/internal/profile/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}

Source = NutritionProfile.TargetsSource

TARGET_FIELDS = (
    "bmr", "daily_kcal", "daily_protein_g", "daily_fat_g", "daily_carbs_g",
    "daily_water_ml", "daily_vitamin_d_iu", "daily_vitamin_b12_mcg",
    "daily_vitamin_c_mg", "daily_iron_mg", "daily_calcium_mg",
    "daily_magnesium_mg", "daily_omega3_g", "daily_fiber_g",
)

FULL_INPUTS = {
    "gender": "male", "age": 41, "height_cm": 185, "weight_kg": 95.0,
    "activity_coefficient": 1.4, "goal": "maintain", "pace": "moderate",
}

LEGACY_TARGETS = dict(
    bmr=1906, daily_kcal=2936, daily_protein_g=133, daily_fat_g=98,
    daily_carbs_g=380, daily_water_ml=2850, daily_vitamin_d_iu=600,
    daily_vitamin_b12_mcg=2.4, daily_vitamin_c_mg=90, daily_iron_mg=8.0,
    daily_calcium_mg=1000, daily_magnesium_mg=400, daily_omega3_g=1.6,
    daily_fiber_g=38,
)


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:nb", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:nb",
    }


def _post(body, headers):
    return APIClient().post(URL, body, format="json", **headers)


def _targets(p):
    return tuple(getattr(p, f) for f in TARGET_FIELDS)


def _refusals(p):
    return [
        e for e in p.last_overrides_applied
        if e.get("reason") == RECOMPUTE_REFUSED_NO_CONSENT
    ]


# ===========================================================================
# 2. Отказ пишет NULL, а не ноль
# ===========================================================================


class TestRefusalWritesNull:
    def test_missing_inputs_with_attestation_leave_null_and_source_none(
        self, proxy_user, headers,
    ):
        """С утверждением, но без веса: расчёта нет — и в базе NULL.

        До части 2 брифа здесь лежали нули: «ориентир 0 ккал» на месте
        «ориентира нет».
        """
        resp = _post({
            "consent": CONSENT,
            "gender": "female", "age": 40, "height_cm": 165,
            "goal": "maintain",
        }, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.daily_kcal is None
        assert p.bmr is None
        assert _targets(p) == (None,) * 14
        assert p.targets_source == Source.NONE
        assert {
            "reason": "insufficient_inputs", "fields": ["weight_kg"],
        } in p.last_overrides_applied
        assert resp.json()["data"]["norms"] == {}
        assert {
            "reason": "insufficient_inputs", "fields": ["weight_kg"],
        } in resp.json()["data"]["overrides_applied"]

    def test_refusal_after_a_computation_clears_to_null_not_zero(
        self, proxy_user, headers,
    ):
        """Был расчёт; следующий POST снял вес — старое число не лежит."""
        _post({"consent": CONSENT, **FULL_INPUTS}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.daily_kcal == 2669  # 1906.25 × 1.4 = 2668.75

        # Тело с параметрами тела и утверждением: вес обнулён явно.
        with patch.object(
            profile_upsert_service, "_apply_patch",
            lambda profile, payload: setattr(profile, "weight_kg", None),
        ):
            _post({"consent": CONSENT, "goal": "maintain"}, headers)
        p.refresh_from_db()
        assert _targets(p) == (None,) * 14
        assert p.targets_source == Source.NONE


# ===========================================================================
# Предикат
# ===========================================================================


class TestPredicate:
    @pytest.mark.parametrize("source", [
        Source.NONE, Source.UNKNOWN_LEGACY, Source.USER_ENTERED,
    ])
    def test_without_attestation_only_ayla_calculated_passes(self, source):
        p = NutritionProfile(targets_source=source)
        assert recompute_permitted(p, {"diet_preference": "vegetarian"}) is False
        assert recompute_permitted(p, {"consent": {"type": PERSONAL_CALCULATION}}) is False
        assert recompute_permitted(p, {"consent": {
            "type": PERSONAL_CALCULATION, "document_version": "  ",
        }}) is False
        assert recompute_permitted(p, {"consent": {
            "type": "health", "document_version": "v1",
        }}) is False

    @pytest.mark.parametrize("source", list(Source))
    def test_with_attestation_every_source_passes(self, source):
        p = NutritionProfile(targets_source=source)
        assert recompute_permitted(p, {"consent": CONSENT}) is True

    def test_ayla_calculated_passes_without_attestation(self):
        p = NutritionProfile(targets_source=Source.AYLA_CALCULATED)
        assert recompute_permitted(p, {"health_flags": {"pregnant": True}}) is True


# ===========================================================================
# 7. Третье окно закрыто
# ===========================================================================


class TestThirdWindowIsClosed:
    """POST без утверждения не воскрешает ориентиры от лежащих входов.

    Оговорка о предмете: сегодня вызывающих с частичным телом (без
    параметров тела и без утверждения) в боте нет — после N-a3 бот
    прикладывает утверждение к каждому POST профиля. Здесь стережётся
    МЕХАНИЗМ, а не наблюдение: сторож обязан держать и того вызывающего,
    которого пока не написали. Запись отказа и строка лога проверяются
    потому, что молчаливый отказ дал бы профиль, который выглядит
    обработанным.
    """

    def _cleared_with_inputs_in_place(self, proxy_user):
        """Состояние после ``clear_targets_without_provenance --apply``
        и ДО ``purge_unconsented_body_parameters --apply``."""
        return NutritionProfile.objects.create(
            user=proxy_user, targets_source=Source.NONE,
            last_overrides_applied=[{"reason": "bmr_floor"}],
            **FULL_INPUTS,
        )

    def test_post_without_attestation_keeps_none_and_null(
        self, proxy_user, headers, caplog,
    ):
        p = self._cleared_with_inputs_in_place(proxy_user)

        with caplog.at_level(logging.WARNING, logger="nutrition.services.profile_upsert_service"):
            resp = _post({"diet_preference": "vegetarian"}, headers)

        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p.refresh_from_db()
        assert p.diet_preference == "vegetarian"  # дневник не закрыт
        assert p.targets_source == Source.NONE
        assert p.daily_kcal is None
        assert _targets(p) == (None,) * 14
        assert p.targets_input_snapshot == {}
        assert resp.json()["data"]["norms"] == {}
        # Отказ — громкий: запись в аудите и строка в логе.
        assert _refusals(p) == [{
            "reason": RECOMPUTE_REFUSED_NO_CONSENT, "targets_source": "none",
        }]
        # Прежний аудит не перезатёрт — запись добавлена.
        assert {"reason": "bmr_floor"} in p.last_overrides_applied
        assert {
            "reason": RECOMPUTE_REFUSED_NO_CONSENT, "targets_source": "none",
        } in resp.json()["data"]["overrides_applied"]
        assert any(
            "nutrition.targets.recompute_refused" in r.getMessage()
            and f"user={proxy_user.id}" in r.getMessage()
            and "source=none" in r.getMessage()
            and r.levelno == logging.WARNING
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_repeated_refusal_is_recorded_once(self, proxy_user, headers):
        p = self._cleared_with_inputs_in_place(proxy_user)
        _post({"diet_preference": "vegetarian"}, headers)
        _post({"diet_preference": "vegan"}, headers)
        p.refresh_from_db()
        assert len(_refusals(p)) == 1

    def test_with_the_guard_disabled_the_window_is_open(
        self, proxy_user, headers,
    ):
        """Целевое доказательство: подмена сторожа → окно открыто.

        Тот же POST, что выше, с ``recompute_permitted → True`` даёт
        ``ayla_calculated`` и число. Это ровно то, что сторож запрещает.
        """
        p = self._cleared_with_inputs_in_place(proxy_user)

        with patch.object(
            profile_upsert_service, "recompute_permitted", lambda *_: True,
        ):
            _post({"diet_preference": "vegetarian"}, headers)

        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.daily_kcal == 2669

    def test_unknown_legacy_is_not_laundered_into_ayla_calculated(
        self, proxy_user, headers,
    ):
        """До очистки: POST без утверждения не переписывает происхождение."""
        p = NutritionProfile.objects.create(
            user=proxy_user, targets_source=Source.UNKNOWN_LEGACY,
            **FULL_INPUTS, **LEGACY_TARGETS,
        )
        before = _targets(p)

        resp = _post({"diet_preference": "vegetarian"}, headers)

        assert resp.status_code == status.HTTP_200_OK
        p.refresh_from_db()
        assert p.targets_source == Source.UNKNOWN_LEGACY
        assert _targets(p) == before
        assert p.targets_computed_at is None
        assert _refusals(p) == [{
            "reason": RECOMPUTE_REFUSED_NO_CONSENT,
            "targets_source": "unknown_legacy",
        }]

    def test_unknown_legacy_with_guard_disabled_would_be_laundered(
        self, proxy_user, headers,
    ):
        p = NutritionProfile.objects.create(
            user=proxy_user, targets_source=Source.UNKNOWN_LEGACY,
            **FULL_INPUTS, **LEGACY_TARGETS,
        )
        with patch.object(
            profile_upsert_service, "recompute_permitted", lambda *_: True,
        ):
            _post({"diet_preference": "vegetarian"}, headers)
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_CALCULATED


# ===========================================================================
# 8. Положительная сторона сторожа
# ===========================================================================


class TestGuardLetsGroundedRecomputeThrough:
    def test_full_inputs_with_attestation_compute_and_snapshot(
        self, proxy_user, headers,
    ):
        resp = _post({"consent": CONSENT, **FULL_INPUTS}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.bmr == 1906
        assert p.daily_kcal == 2669
        assert p.targets_input_snapshot["weight_kg"] == 95.0
        assert p.targets_method_versions == {"calories": "mifflin_st_jeor_v1"}
        assert p.targets_computed_at is not None
        assert _refusals(p) == []
        assert resp.json()["data"]["norms"]["daily_kcal"] == 2669

    def test_health_flag_change_on_ayla_calculated_recomputes_without_attestation(
        self, proxy_user, headers,
    ):
        """Сценарий (б): расчёт состоялся с утверждением; смена флага
        здоровья пересчитывает от тех же входов, происхождение остаётся."""
        _post({"consent": CONSENT, **FULL_INPUTS}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        first_at = p.targets_computed_at
        assert p.daily_iron_mg == 8.0

        resp = _post({"health_flags": {"pregnant": True}}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()

        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.daily_iron_mg == 27.0  # RDA беременности — пересчёт состоялся
        assert p.daily_kcal == 2869  # 2668.75 + 200
        assert p.targets_computed_at >= first_at
        assert _refusals(p) == []

    def test_attestation_alone_unlocks_recompute_on_a_cleared_row(
        self, proxy_user, headers,
    ):
        """После очистки входы на месте; утверждение — и расчёт состоялся.

        Это «повторный расчёт только после согласия» из §103 дословно.
        """
        p = NutritionProfile.objects.create(
            user=proxy_user, targets_source=Source.NONE, **FULL_INPUTS,
        )
        _post({"diet_preference": "vegetarian"}, headers)
        p.refresh_from_db()
        assert p.targets_source == Source.NONE

        resp = _post({"consent": CONSENT, "goal": "maintain"}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.daily_kcal == 2669
        # Состоявшийся пересчёт пишет аудит заново — отказа в нём нет.
        assert _refusals(p) == []
        assert isinstance(p.targets_computed_at, datetime)
        assert p.targets_computed_at.tzinfo is not None
        assert p.targets_computed_at <= datetime.now(dt_tz.utc)
