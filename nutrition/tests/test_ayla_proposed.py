"""Предложенный ориентир: ``ayla_proposed`` до подтверждения человеком (§5.1).

Решение владельца 11.09.2026 §5.1: «Ayla может показать предварительный
ориентир только внутри добровольного расчёта… Результат становится
привычкой/шагом только после подтверждения пользователя». Здесь это
состояние источника, а не флаг (флаг рядом с источником — две правды об
одном числе); подтверждение переводит его в ``ayla_calculated``; в оценки
и «осталось» предложение не входит — как отсутствие.

Оговорка о предмете: после #1523 анкета на пилоте закрыта fail-closed до
экрана согласия, и предложений сегодня не получит никто. Стережём
МЕХАНИЗМ, а не наблюдение.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import NutritionProfile
from nutrition.services import profile_upsert_service
from nutrition.services.nutrition_summary_service import _compute_goal_progress
from nutrition.services.pattern_detection_service import (
    _profile_goal_kcal,
    _profile_goal_protein,
)
from nutrition.services.personal_calculation_consent import PERSONAL_CALCULATION
from nutrition.services.returning_success_service import _resolve_goal
from nutrition.services.targets_state import CONFIRMED_SOURCES, targets_confirmed
from users.models import User

pytestmark = pytest.mark.django_db

URL = "/api/v1/nutrition/internal/profile/"
CONFIRM_URL = "/api/v1/nutrition/internal/profile/targets/confirm/"
CONSENT = {"type": PERSONAL_CALCULATION, "document_version": "v1"}
SERVICE_TOKEN = "svc-token-proposed"
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
    return User.objects.create(username="bot:proposed", role="client", is_proxy=True)


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:proposed",
    }


def _post(body, headers):
    return APIClient().post(URL, body, format="json", **headers)


def _confirm(headers):
    return APIClient().post(CONFIRM_URL, {}, format="json", **headers)


def _compute(proxy_user, headers):
    resp = _post({**FULL_INPUTS, "consent": CONSENT}, headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return NutritionProfile.objects.get(user=proxy_user)


# ===========================================================================
# Расчёт — предложение, не действующий ориентир
# ===========================================================================


class TestComputationIsAProposal:
    def test_a_fresh_computation_lands_as_ayla_proposed(self, proxy_user, headers):
        resp = _post({**FULL_INPUTS, "consent": CONSENT}, headers)
        body = resp.json()["data"]
        p = NutritionProfile.objects.get(user=proxy_user)

        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.targets_confirmed_at is None
        assert p.daily_kcal is not None and p.daily_kcal > 0  # число ЕСТЬ
        # Наружу: число показывается (предложение), происхождение говорит,
        # что оно не подтверждено.
        assert body["norms"]["daily_kcal"] == p.daily_kcal
        assert body["targets_provenance"]["source"] == "ayla_proposed"
        assert body["targets_provenance"]["confirmed_at"] is None
        assert body["targets_provenance"]["input_snapshot"]["weight_kg"] == 67.0

    def test_a_recompute_on_a_confirmed_row_is_a_new_proposal(self, proxy_user, headers):
        """Подтверждение относится к числам, которые человек видел."""
        _compute(proxy_user, headers)
        assert _confirm(headers).status_code == status.HTTP_200_OK
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.targets_confirmed_at is not None

        # Смена темпа (открытое поле, сценарий (б) сторожа — пересчёт без
        # утверждения разрешён); результат — НОВОЕ предложение.
        kcal_before = p.daily_kcal
        resp = _post({"pace": "gentle"}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.targets_confirmed_at is None
        assert p.daily_kcal != kcal_before  # пересчёт состоялся
        assert p.targets_input_snapshot["pace"] == "gentle"

    def test_a_refusal_clears_confirmation_too(self, proxy_user, headers):
        """Расчёт снят (входа не хватило) — подтверждение снято вместе с ним."""
        _compute(proxy_user, headers)
        _confirm(headers)
        # Сериализатор не пропускает ``weight_kg: null``; вес снимается так
        # же, как в ``test_targets_recompute_gate``: подменой патча.
        with patch.object(
            profile_upsert_service, "_apply_patch",
            lambda profile, payload: setattr(profile, "weight_kg", None),
        ):
            resp = _post({"goal": "maintain", "consent": CONSENT}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.NONE
        assert p.targets_confirmed_at is None
        assert p.daily_kcal is None

    def test_a_partial_post_on_a_proposal_recomputes_and_stays_a_proposal(
        self, proxy_user, headers,
    ):
        """(б) сторожа распространяется на ayla_proposed: основание то же."""
        p0 = _compute(proxy_user, headers)
        resp = _post({"pace": "gentle"}, headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal != p0.daily_kcal


# ===========================================================================
# Подтверждение
# ===========================================================================


class TestConfirmation:
    def test_confirm_moves_proposed_to_calculated_and_keeps_the_numbers(
        self, proxy_user, headers,
    ):
        p = _compute(proxy_user, headers)
        before = (
            p.daily_kcal, p.bmr, dict(p.targets_input_snapshot), p.targets_computed_at,
        )

        resp = _confirm(headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["confirmation"] == {"outcome": "confirmed"}
        assert body["targets_provenance"]["source"] == "ayla_calculated"
        assert body["targets_provenance"]["confirmed_at"] is not None

        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_CALCULATED
        assert p.targets_confirmed_at is not None
        # Подтверждено ТО, что предложено: ни числа, ни снимок, ни момент
        # расчёта не изменились.
        after = (
            p.daily_kcal, p.bmr, dict(p.targets_input_snapshot), p.targets_computed_at,
        )
        assert after == before

    def test_second_confirm_is_a_no_op_with_its_own_name(self, proxy_user, headers):
        _compute(proxy_user, headers)
        first = _confirm(headers).json()["data"]["targets_provenance"]["confirmed_at"]
        resp = _confirm(headers)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["data"]["confirmation"] == {"outcome": "already_confirmed"}
        # Момент подтверждения не переписан: повтор кнопки — не более
        # позднее решение.
        assert resp.json()["data"]["targets_provenance"]["confirmed_at"] == first

    @pytest.mark.parametrize(
        "source",
        [Source.NONE, Source.UNKNOWN_LEGACY, Source.USER_ENTERED],
    )
    def test_nothing_to_confirm_is_a_named_refusal(self, proxy_user, headers, source):
        NutritionProfile.objects.create(
            user=proxy_user, targets_source=source, daily_kcal=1700, **FULL_INPUTS,
        )
        resp = _confirm(headers)
        assert resp.status_code == status.HTTP_409_CONFLICT, resp.json()
        err = resp.json()["error"]
        assert err["code"] == "NOTHING_TO_CONFIRM"
        assert err["details"]["targets_source"] == source
        p = NutritionProfile.objects.get(user=proxy_user)
        assert p.targets_source == source  # ничего не сдвинуто
        assert p.targets_confirmed_at is None

    def test_no_profile_is_nothing_to_confirm(self, proxy_user, headers):
        resp = _confirm(headers)
        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.json()["error"]["details"]["targets_source"] == "none"


# ===========================================================================
# Предложение не участвует в оценках — как отсутствие
# ===========================================================================


class TestProposalDoesNotCount:
    """Один предикат на всех читателей: ``targets_confirmed``.

    Множество действующих источников совпадает с ботом
    (``targets_are_configured`` ⇔ {ayla_calculated, user_entered}) и не
    расширяется.
    """

    def test_confirmed_sources_are_exactly_the_two(self):
        assert CONFIRMED_SOURCES == frozenset({"ayla_calculated", "user_entered"})
        assert targets_confirmed(None) is False

    def _row(self, proxy_user, source):
        return NutritionProfile.objects.create(
            user=proxy_user, targets_source=source,
            daily_kcal=1600, daily_protein_g=100, daily_iron_mg=18.0,
            **FULL_INPUTS,
        )

    def test_proposed_is_absent_for_every_value_reader(self, proxy_user):
        p = self._row(proxy_user, Source.AYLA_PROPOSED)
        assert targets_confirmed(p) is False
        assert _compute_goal_progress(p) is None
        assert _profile_goal_kcal(p) == 0.0
        assert _profile_goal_protein(p) == 0.0
        assert _resolve_goal(p) == 0

    @pytest.mark.parametrize("source", [Source.AYLA_CALCULATED, Source.USER_ENTERED])
    def test_confirmed_counts_positive_guard(self, proxy_user, source):
        """POSITIVE: с действующим источником те же читатели видят число."""
        p = self._row(proxy_user, source)
        assert targets_confirmed(p) is True
        progress = _compute_goal_progress(p)
        assert progress is not None and progress["current_kcal_target"] == 1600
        assert _profile_goal_kcal(p) == 1600.0
        assert _profile_goal_protein(p) == 100.0
        assert _resolve_goal(p) == 1600

    def test_clear_command_leaves_a_proposal_untouched(self, proxy_user):
        """Команда очистки трогает unknown_legacy и остатки none — не предложения."""
        p = self._row(proxy_user, Source.AYLA_PROPOSED)
        out = StringIO()
        call_command("clear_targets_without_provenance", "--apply", stdout=out)
        p.refresh_from_db()
        assert p.targets_source == Source.AYLA_PROPOSED
        assert p.daily_kcal == 1600
