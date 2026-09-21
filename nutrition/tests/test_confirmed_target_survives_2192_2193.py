"""Действующий ориентир не исчезает от нового расчёта (DRF-2192, DRF-2193).

Требования владельца (CURRENT_DECISIONS §63, 21.09.2026; §5.1):

* DRF-2192 — «старый подтверждённый ориентир не исчезает до подтверждения
  нового». Сегодня пересчёт на строке ``ayla_calculated`` пишет новые числа
  ПОВЕРХ подтверждённых, ставит ``ayla_proposed`` и стирает ``confirmed_at``:
  между «вес изменился» и «человек подтвердил» у него нет действующего
  ориентира вовсе — «осталось на сегодня» и оценки гаснут (§6).
* DRF-2193 — «вес может обновляться без уничтожения ручного ориентира».
  Параметры тела принимаются только с утверждением о согласии, а
  утверждение само разрешает пересчёт (``recompute_permitted``, сценарий
  (а)) — поэтому ручная норма заменяется расчётной ровно тогда, когда
  человек просто сообщил новый вес.

Истина — по видам (DRF-1929): вид с подписью ``ayla_calculated`` или
``user_entered`` действует, и пересчёт не имеет права его заменить.

* c1 — подтверждённый расчёт + новый вес: действующие калории/вода, их
  подпись и ``confirmed_at`` на месте; новое предложение лежит РЯДОМ
  (``targets_provenance.pending_proposal``), а не вместо;
* c2 — подтверждение забирает лежащее рядом предложение в действующее;
* m1 — ручные калории + новый вес: вес записан, калории человека и их
  подпись на месте;
* m2 — смешанная строка (калории посчитаны и подтверждены, вода задана
  рукой): новый вес не трогает ни один действующий вид.

Узлы ревью (граница правила):

* r1 — отказ расчёта по здоровью (беременность) гасит расчётный вид, как
  до DRF-2192: §63 про вес, а не про то, чтобы держать дефицит;
* r2 — отзыв согласия стирает и предложение рядом (в нём снимок входов),
  подписи по видам уходят в ``none``, и следующий расчёт ложится на место;
* r3 — запрос без пересчёта, тронувший вход, снимает устаревшее
  предложение: подтверждение не вернёт старый темп поверх нового;
* r4 — ручная вода, затем анкета: калории — предложение на месте, и общая
  подпись говорит «предложено», а не «действует» (бот читает её);
* r5 — подтверждение на смешанной строке не переписывает отметку, которую
  ставил человек, и повтор — «нечего подтверждать», а не новое событие;
* r6 — предложение рядом несёт цель, темп и переопределения: человек видит
  всё, что подтверждение применит.
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from nutrition.services.personal_calculation_withdrawal import (
    erase_personal_calculation_inputs,
)
from nutrition.services.targets_state import calories_confirmed, fluids_confirmed
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
URL_CONFIRM = "/api/v1/nutrition/internal/profile/targets/confirm/"
URL_MANUAL = "/api/v1/nutrition/internal/profile/targets/manual/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-2192"
Source = NutritionProfile.TargetsSource

FULL_INPUTS = {
    "gender": "female", "age": 36, "height_cm": 170, "weight_kg": 67.0,
    # Вопрос 59: при «похудеть» темп называется — умолчания больше нет.
    "activity_coefficient": 1.4, "goal": "lose", "pace": "moderate",
}


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    return User.objects.create(username="bot:2192", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:2192",
    }


def _post(body, headers):
    resp = APIClient().post(URL, body, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _confirm(headers):
    resp = APIClient().post(URL_CONFIRM, {}, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _manual(body, headers):
    resp = APIClient().post(URL_MANUAL, body, format="json", **headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["data"]


def _confirmed_calculation(proxy_user, headers) -> NutritionProfile:
    _post({**FULL_INPUTS, "consent": CONSENT}, headers)
    _confirm(headers)
    p = NutritionProfile.objects.get(user=proxy_user)
    # Присутствие: действующий расчёт есть — дальше проверяется, что он
    # переживает новый вес, а не что его не было.
    assert p.calories_source == Source.AYLA_CALCULATED
    assert p.calories_confirmed_at is not None
    assert p.daily_kcal and p.daily_kcal > 0
    return p


def _new_weight(headers, kg: float = 61.0) -> dict:
    return _post({"weight_kg": kg, "consent": CONSENT}, headers)


# ===========================================================================
# DRF-2192 — подтверждённый расчёт живёт до подтверждения нового
# ===========================================================================


class TestConfirmedCalculationSurvivesANewWeight:
    def test_active_target_stays_and_the_proposal_lies_beside_it(self, proxy_user, headers):
        before = _confirmed_calculation(proxy_user, headers)

        body = _new_weight(headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        # Действующее — то же число, та же подпись, то же время подтверждения.
        assert p.weight_kg == 61.0
        assert p.daily_kcal == before.daily_kcal
        assert p.daily_water_ml == before.daily_water_ml
        assert p.calories_source == Source.AYLA_CALCULATED
        assert p.fluids_source == Source.AYLA_CALCULATED
        assert p.calories_confirmed_at == before.calories_confirmed_at
        assert calories_confirmed(p) and fluids_confirmed(p)
        assert body["norms"]["daily_kcal"] == before.daily_kcal

        # Новое предложение — рядом и видно, а не потеряно.
        pending = body["targets_provenance"]["pending_proposal"]
        assert pending is not None
        assert pending["daily_kcal"] != before.daily_kcal
        assert pending["input_snapshot"]["weight_kg"] == 61.0

    def test_confirmation_takes_the_proposal_that_lay_beside(self, proxy_user, headers):
        before = _confirmed_calculation(proxy_user, headers)
        proposed = _new_weight(headers)["targets_provenance"]["pending_proposal"]

        body = _confirm(headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.daily_kcal == proposed["daily_kcal"]
        assert p.daily_kcal != before.daily_kcal
        assert p.calories_source == Source.AYLA_CALCULATED
        assert p.calories_confirmed_at > before.calories_confirmed_at
        assert p.targets_input_snapshot["weight_kg"] == 61.0
        assert body["targets_provenance"]["pending_proposal"] is None


# ===========================================================================
# DRF-2193 — новый вес не уничтожает ручной ориентир
# ===========================================================================


class TestManualTargetSurvivesANewWeight:
    def test_weight_is_stored_and_manual_calories_stay(self, proxy_user, headers):
        _post({**FULL_INPUTS, "consent": CONSENT}, headers)
        _manual({"calories_kcal": 1800}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        # Присутствие: ручная норма стоит.
        assert p.calories_source == Source.USER_ENTERED and p.daily_kcal == 1800

        body = _new_weight(headers)
        p.refresh_from_db()

        assert p.weight_kg == 61.0
        assert p.daily_kcal == 1800
        assert p.calories_source == Source.USER_ENTERED
        assert p.calories_confirmed_at is not None
        assert calories_confirmed(p)
        assert body["norms"]["daily_kcal"] == 1800

    def test_mixed_row_keeps_every_active_kind(self, proxy_user, headers):
        before = _confirmed_calculation(proxy_user, headers)
        _manual({"water_ml": 2000}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        # Присутствие: калории — расчёт, вода — рукой; оба действуют.
        assert p.calories_source == Source.AYLA_CALCULATED
        assert p.fluids_source == Source.USER_ENTERED and p.daily_water_ml == 2000

        _new_weight(headers)
        p.refresh_from_db()

        assert p.weight_kg == 61.0
        assert p.daily_water_ml == 2000
        assert p.fluids_source == Source.USER_ENTERED
        assert p.calories_source == Source.AYLA_CALCULATED
        assert p.daily_kcal == before.daily_kcal


# ===========================================================================
# Узлы ревью — граница правила
# ===========================================================================


class TestTheRuleIsAboutWeightNotAboutSafety:
    def test_a_health_refusal_still_puts_out_a_calculated_target(self, proxy_user, headers):
        before = _confirmed_calculation(proxy_user, headers)
        assert before.daily_kcal is not None

        _post({"health_flags": {"pregnant": True}}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.calories_source == Source.NONE
        assert p.daily_kcal is None
        assert p.calories_confirmed_at is None
        assert p.pending_proposal is None
        assert any(
            e.get("reason") == "health_factor_pregnant" for e in p.last_overrides_applied
        )

    def test_a_health_refusal_keeps_the_persons_own_number(self, proxy_user, headers):
        _post({**FULL_INPUTS, "consent": CONSENT}, headers)
        _manual({"calories_kcal": 1800}, headers)

        _post({"health_flags": {"pregnant": True}, "consent": CONSENT}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        # Ручное число — не расчёт: отказ методики его не отменяет.
        assert p.calories_source == Source.USER_ENTERED
        assert p.daily_kcal == 1800


class TestWithdrawalErasesTheProposalToo:
    def test_withdrawal_leaves_no_proposal_and_the_next_calculation_lands_in_place(
        self, proxy_user, headers
    ):
        _confirmed_calculation(proxy_user, headers)
        pending = _new_weight(headers)["targets_provenance"]["pending_proposal"]
        # Присутствие: предложение рядом лежит — и несёт вес.
        assert pending["input_snapshot"]["weight_kg"] == 61.0

        erase_personal_calculation_inputs(proxy_user)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.pending_proposal is None
        assert p.calories_source == Source.NONE
        assert p.fluids_source == Source.NONE
        assert p.calories_confirmed_at is None

        body = _post({**FULL_INPUTS, "consent": CONSENT}, headers)
        p.refresh_from_db()
        assert p.calories_source == Source.AYLA_PROPOSED
        assert p.daily_kcal is not None
        assert body["targets_provenance"]["pending_proposal"] is None


class TestAStaleProposalDoesNotSurvive:
    def test_a_refused_patch_that_touched_an_input_drops_the_proposal(
        self, proxy_user, headers
    ):
        _confirmed_calculation(proxy_user, headers)
        _manual({"water_ml": 2000}, headers)
        # Смешанная строка: общая подпись — ручная, сторож без утверждения
        # пересчёт не пустит.
        _new_weight(headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.pending_proposal is not None

        _post({"pace": "gentle"}, headers)  # без утверждения — отказ
        p.refresh_from_db()

        assert p.pace == "gentle"
        assert p.pending_proposal is None
        resp = APIClient().post(URL_CONFIRM, {}, format="json", **headers)
        # Подтверждать нечего: старое предложение не вернёт прежний темп.
        assert resp.status_code == status.HTTP_409_CONFLICT
        p.refresh_from_db()
        assert p.pace == "gentle"


class TestTheOverallLabelTellsTheTruth:
    def test_manual_water_then_a_questionnaire_reads_as_proposed(self, proxy_user, headers):
        _manual({"water_ml": 2000}, headers)
        body = _post({**FULL_INPUTS, "consent": CONSENT}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.fluids_source == Source.USER_ENTERED and p.daily_water_ml == 2000
        assert p.calories_source == Source.AYLA_PROPOSED
        # Бот читает общую подпись: калории-предложение не должны читаться
        # как действующий ориентир (§6).
        assert body["targets_provenance"]["source"] == "ayla_proposed"
        assert p.targets_confirmed_at is None


class TestConfirmOnAMixedRow:
    def test_confirm_takes_calories_and_leaves_the_persons_stamp(self, proxy_user, headers):
        _confirmed_calculation(proxy_user, headers)
        _manual({"water_ml": 2000}, headers)
        p = NutritionProfile.objects.get(user=proxy_user)
        manual_stamp = p.targets_confirmed_at
        fluids_stamp = p.fluids_confirmed_at
        proposed = _new_weight(headers)["targets_provenance"]["pending_proposal"]

        _confirm(headers)
        p.refresh_from_db()

        assert p.daily_kcal == proposed["daily_kcal"]
        assert p.daily_water_ml == 2000
        assert p.fluids_source == Source.USER_ENTERED
        assert p.fluids_confirmed_at == fluids_stamp
        assert p.targets_source == Source.USER_ENTERED
        assert p.targets_confirmed_at == manual_stamp

        # Повтор — не новое событие: подтверждать нечего.
        resp = APIClient().post(URL_CONFIRM, {}, format="json", **headers)
        assert resp.status_code == status.HTTP_409_CONFLICT


class TestTheProposalShowsWhatConfirmationApplies:
    def test_goal_pace_and_overrides_ride_with_the_proposal(self, proxy_user, headers):
        _confirmed_calculation(proxy_user, headers)
        pending = _post(
            {"weight_kg": 61.0, "pace": "gentle", "consent": CONSENT}, headers
        )["targets_provenance"]["pending_proposal"]

        assert pending["pace"] == "gentle"
        assert pending["goal"] == "lose"
        assert "overrides_applied" in pending
        assert isinstance(pending["computed_at"], str) and pending["computed_at"].endswith("Z")
